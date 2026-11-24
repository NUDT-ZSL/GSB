from __future__ import annotations

import hashlib
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import Condition, Event
from typing import Any, Callable

from .config import (
    TaskConfig,
    TrackSpec,
    choose_block_frames,
    load_task,
)
from .dsp import (
    chunk_audio_range,
    extract_fingerprint_chunk,
    fingerprint_chunk_version,
    frame_count,
)
from .errors import AudioFPError, TaskInterrupted, fail
from .index import load_index, rebuild_index, reference_signature
from .search import (
    aggregate_query_candidates,
    build_candidate_payloads,
    candidate_chunk_version,
)
from .store import ChunkStore
from .util import atomic_write_json, sha256_value
from .wavio import pcm_bytes_for_range


@dataclass
class StopController:
    requested_signum: int | None = None

    def request(self, signum: int) -> None:
        if self.requested_signum is None:
            self.requested_signum = int(signum)

    @property
    def requested(self) -> bool:
        return self.requested_signum is not None

    @property
    def exit_code(self) -> int:
        import signal

        return 130 if self.requested_signum == signal.SIGINT else 143

    @property
    def signal_name(self) -> str:
        import signal

        return "SIGINT" if self.requested_signum == signal.SIGINT else "SIGTERM"


class MemoryBudget:
    def __init__(self, total: int) -> None:
        self.total = int(total)
        self.used = 0
        self.condition = Condition()

    def acquire(self, amount: int, stop: StopController | None = None) -> bool:
        amount = int(amount)
        with self.condition:
            while self.used + amount > self.total:
                if stop is not None and stop.requested:
                    return False
                self.condition.wait(timeout=0.05)
            self.used += amount
            return True

    def release(self, amount: int) -> None:
        with self.condition:
            self.used -= int(amount)
            if self.used < 0:
                self.used = 0
            self.condition.notify_all()


@dataclass(frozen=True)
class ChunkPlan:
    start: int
    end: int
    version: str


@dataclass
class TrackPlan:
    track: TrackSpec
    frame_total: int
    chunks: list[ChunkPlan]


@dataclass(frozen=True)
class FingerprintJob:
    order: int
    track: TrackSpec
    chunk: ChunkPlan
    cost: int


@dataclass(frozen=True)
class CandidateWindow:
    start: int
    end: int
    dependency_version: str
    dependencies: tuple[tuple[int, int, str], ...]


def _frame_chunks(config: TaskConfig, track: TrackSpec) -> TrackPlan:
    total = frame_count(
        track.info.sample_count,
        config.fingerprint.frame_length,
        config.fingerprint.hop_length,
    )
    effective_chunk = min(
        config.chunk_frames,
        choose_block_frames(
            config.fingerprint, config.chunk_frames, config.memory_budget_bytes
        ),
    )
    chunks: list[ChunkPlan] = []
    for start in range(0, total, effective_chunk):
        end = min(total, start + effective_chunk)
        version = fingerprint_chunk_version(
            track.info, config.fingerprint, start, end
        )
        chunks.append(ChunkPlan(start=start, end=end, version=version))
    return TrackPlan(track=track, frame_total=total, chunks=chunks)


def _candidate_windows(config: TaskConfig, plan: TrackPlan) -> list[CandidateWindow]:
    windows: list[CandidateWindow] = []
    for start in range(0, plan.frame_total, config.chunk_frames):
        end = min(plan.frame_total, start + config.chunk_frames)
        dependencies = tuple(
            (chunk.start, chunk.end, chunk.version)
            for chunk in plan.chunks
            if chunk.start < end and chunk.end > start
        )
        dependency_version = sha256_value(
            {
                "kind": "candidate_query_window_v1",
                "frame_interval": [start, end],
                "fingerprint_chunks": [list(item) for item in dependencies],
            }
        )
        windows.append(
            CandidateWindow(
                start=start,
                end=end,
                dependency_version=dependency_version,
                dependencies=dependencies,
            )
        )
    return windows


def _fingerprint_cost(config: TaskConfig, chunk: ChunkPlan) -> int:
    bins = config.fingerprint.frame_length // 2 + 1
    context = (chunk.end - chunk.start) + config.fingerprint.fan_out_frames
    pair_count = config.fingerprint.peaks_per_frame * config.fingerprint.pair_limit_per_peak
    return int(context * bins * 8 + (chunk.end - chunk.start) * pair_count * 1024)


def _fingerprint_worker(
    config: TaskConfig, store: ChunkStore, job: FingerprintJob
) -> dict[str, Any]:
    try:
        items = extract_fingerprint_chunk(
            job.track.info,
            config.fingerprint,
            job.chunk.start,
            job.chunk.end,
        )
        sample_start, sample_end = chunk_audio_range(
            job.track.info,
            config.fingerprint,
            job.chunk.start,
            job.chunk.end,
        )
        payload = {
            "schema_version": 1,
            "track": {
                "track_id": job.track.track_id,
                "file": job.track.info.label,
                "kind": job.track.kind,
                "order": job.track.order,
                "sample_rate": job.track.info.sample_rate,
                "sample_count": job.track.info.sample_count,
            },
            "dependencies": {
                "frame_interval": [job.chunk.start, job.chunk.end],
                "sample_interval": [sample_start, sample_end],
                "band_boundaries_hz": list(
                    config.fingerprint.band_boundaries_hz
                ),
                "peak_neighborhood": config.fingerprint.peak_neighborhood,
                "peaks_per_frame": config.fingerprint.peaks_per_frame,
                "fan_out_frames": config.fingerprint.fan_out_frames,
                "fingerprint_version": config.fingerprint.version,
            },
            "items": items,
        }
        logical = (
            f"fp/{job.track.track_id}/{job.chunk.start:08d}_{job.chunk.end:08d}"
        )
        store.write(logical, job.chunk.version, payload)
        return {
            "track_id": job.track.track_id,
            "start": job.chunk.start,
            "end": job.chunk.end,
            "item_count": len(items),
        }
    except AudioFPError:
        raise
    except Exception as exc:
        raise AudioFPError(
            "E_TRACK_PROCESSING",
            "指纹提取或分块写入失败",
            track=job.track.info.label,
            frame_interval=[job.chunk.start, job.chunk.end],
            error=str(exc),
        ) from exc


def _track_input_manifest(track: TrackSpec) -> dict[str, Any]:
    return {
        "kind": track.kind,
        "file": track.info.label,
        "sample_rate": track.info.sample_rate,
        "sample_count": track.info.sample_count,
        "pcm_sha256": track.info.pcm_sha256,
    }


@lru_cache(maxsize=64)
def _prefix_matches(config: TaskConfig, track: TrackSpec, count: int, expected_sha: str) -> bool:
    data = pcm_bytes_for_range(track.info, 0, count)
    return hashlib.sha256(data).hexdigest() == expected_sha


def _fingerprint_reason(
    config: TaskConfig,
    track: TrackSpec,
    chunk: ChunkPlan,
    status: str,
    previous: dict[str, Any] | None,
    *,
    force_full: bool,
    was_interrupted: bool,
) -> str:
    if force_full:
        return "forced_full_recompute"
    if previous is not None and previous.get("fingerprint") != config.fingerprint.canonical():
        return "fingerprint_params_changed"
    if status == "invalid":
        return "invalid_chunk_digest"
    old_inputs = (previous or {}).get("inputs", {}).get(track.track_id)
    if old_inputs:
        old_count = int(old_inputs.get("sample_count", 0))
        old_sha = str(old_inputs.get("pcm_sha256", ""))
        if old_count < track.info.sample_count and old_sha:
            if _prefix_matches(config, track, old_count, old_sha):
                return "appended_audio_tail"
    if status == "missing":
        return "missing_chunk_after_interruption" if was_interrupted else "cold_cache"
    if status == "version_conflict":
        if old_inputs:
            if old_count == track.info.sample_count and old_sha == track.info.pcm_sha256:
                return "chunk_version_changed"
            return "source_content_changed"
        return "version_conflict"
    return "cache_refresh"


def _candidate_reason(
    config: TaskConfig,
    previous: dict[str, Any] | None,
    *,
    status: str,
    query_track_id: str,
    query_chunk_written: bool,
    reference_version_changed: bool,
    force_full: bool,
    was_interrupted: bool,
) -> str:
    if force_full:
        return "forced_full_recompute"
    if previous is not None and previous.get("search") != config.search.canonical():
        return "search_params_changed"
    if status == "invalid":
        return "invalid_chunk_digest"
    if (
        previous is not None
        and query_track_id not in previous.get("inputs", {})
    ):
        return "new_query_track"
    if previous is not None and query_chunk_written:
        return "query_fingerprint_changed"
    if previous is not None and reference_version_changed:
        return "reference_changed"
    if status == "missing":
        return "missing_chunk_after_interruption" if was_interrupted else "cold_cache"
    return "candidate_version_changed"


class SearchRunner:
    def __init__(
        self,
        config: TaskConfig,
        *,
        force_full: bool = False,
        stop: StopController | None = None,
        progress: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.config = config
        self.force_full = force_full
        self.stop = stop
        self.progress = progress
        self.store = ChunkStore(config.state_dir)
        self.previous_manifest = self.store.read_manifest()
        self.interruption = self.store.read_interruption()
        self.affected: list[dict[str, Any]] = []
        self.chunks_completed = 0
        self.fp_written = 0
        self.fp_reused = 0
        self.candidate_written = 0
        self.candidate_reused = 0
        self.fresh_query_chunks: set[tuple[str, int, int]] = set()
        self.fp_versions: dict[str, dict[tuple[int, int], str]] = {}
        self.candidate_versions: dict[str, str] = {}
        self.reference_versions: dict[str, str] = {}
        self.trace_paths: dict[str, str] = {}
        self.index_info: dict[str, Any] = {}
        self._test_interrupt_after = self._read_test_interrupt_limit()

    @staticmethod
    def _read_test_interrupt_limit() -> int | None:
        raw = os.environ.get("AUDIOFP_INTERRUPT_AFTER_CHUNKS")
        if raw is None:
            return None
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value > 0 else None

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self.progress is not None:
            self.progress(event, data)

    def _checkpoint(
        self,
        phase: str,
        track: TrackSpec | None = None,
        frame_interval: list[int] | None = None,
    ) -> None:
        delay_raw = os.environ.get("AUDIOFP_TEST_CHUNK_DELAY_MS")
        if delay_raw:
            try:
                delay_ms = max(0, int(delay_raw))
            except ValueError:
                delay_ms = 0
            if delay_ms:
                time.sleep(delay_ms / 1000.0)
        signum = self.stop.requested if self.stop is not None else None
        hook = (
            self._test_interrupt_after is not None
            and self.chunks_completed >= self._test_interrupt_after
        )
        if not signum and not hook:
            return
        if signum:
            exit_code = self.stop.exit_code if self.stop is not None else 130
            signal_name = self.stop.signal_name if self.stop is not None else "SIGINT"
        else:
            exit_code = 130
            signal_name = "TEST_CHUNK_BOUNDARY"
        self.store.write_interruption(
            exit_code=exit_code,
            signal_name=signal_name,
            phase=phase,
            track=track.info.label if track is not None else None,
            frame_interval=frame_interval,
            chunks_completed=self.chunks_completed,
        )
        raise TaskInterrupted(exit_code=exit_code, signal_name=signal_name)

    def _record_affected(
        self,
        phase: str,
        track: TrackSpec,
        start: int,
        end: int,
        reason: str,
    ) -> None:
        sample_start = start * self.config.fingerprint.hop_length
        dep_end_frame = min(
            frame_count(
                track.info.sample_count,
                self.config.fingerprint.frame_length,
                self.config.fingerprint.hop_length,
            ),
            end + self.config.fingerprint.fan_out_frames,
        )
        sample_end = min(
            track.info.sample_count,
            max(
                sample_start,
                dep_end_frame * self.config.fingerprint.hop_length
                + self.config.fingerprint.frame_length,
            ),
        )
        self.affected.append(
            {
                "phase": phase,
                "track": track.info.label,
                "track_id": track.track_id,
                "frame_interval": [int(start), int(end)],
                "sample_interval": [int(sample_start), int(sample_end)],
                "reason": reason,
            }
        )

    def _run_fingerprint_phase(
        self, plans: list[TrackPlan]
    ) -> None:
        jobs: list[FingerprintJob] = []
        job_order = 0
        for plan in plans:
            versions: dict[tuple[int, int], str] = {}
            for chunk in plan.chunks:
                logical = (
                    f"fp/{plan.track.track_id}/{chunk.start:08d}_{chunk.end:08d}"
                )
                result = self.store.read_valid(logical, chunk.version)
                versions[(chunk.start, chunk.end)] = chunk.version
                if result.status == "valid" and not self.force_full:
                    self.fp_reused += 1
                    self.chunks_completed += 1
                    self._checkpoint(
                        "fingerprint_cache",
                        plan.track,
                        [chunk.start, chunk.end],
                    )
                    continue
                reason = _fingerprint_reason(
                    self.config,
                    plan.track,
                    chunk,
                    result.status,
                    self.previous_manifest,
                    force_full=self.force_full,
                    was_interrupted=self.interruption is not None,
                )
                self._record_affected(
                    "fingerprint", plan.track, chunk.start, chunk.end, reason
                )
                jobs.append(
                    FingerprintJob(
                        order=job_order,
                        track=plan.track,
                        chunk=chunk,
                        cost=_fingerprint_cost(self.config, chunk),
                    )
                )
                job_order += 1
                if plan.track.kind == "query":
                    self.fresh_query_chunks.add(
                        (plan.track.track_id, chunk.start, chunk.end)
                    )
            self.fp_versions[plan.track.track_id] = versions

        if not jobs:
            self._checkpoint("fingerprint_complete")
            return

        budget = MemoryBudget(self.config.memory_budget_bytes)
        errors_by_order: dict[int, AudioFPError] = {}
        error_event = Event()
        interrupted: TaskInterrupted | None = None
        futures: dict[Future[dict[str, Any]], FingerprintJob] = {}
        executor = ThreadPoolExecutor(
            max_workers=self.config.concurrency_limit,
            thread_name_prefix="audiofp-fp",
        )

        def on_complete(
            completed: Future[dict[str, Any]],
            job: FingerprintJob,
            amount: int,
        ) -> None:
            budget.release(amount)
            if completed.cancelled():
                return
            try:
                exception = completed.exception()
            except Exception:
                return
            if exception is None:
                return
            if isinstance(exception, AudioFPError):
                error = exception
            else:
                error = AudioFPError(
                    "E_TRACK_PROCESSING",
                    "并发指纹任务异常",
                    track=job.track.info.label,
                    error=str(exception),
                )
            errors_by_order.setdefault(job.order, error)
            error_event.set()

        try:
            for job in jobs:
                if error_event.is_set():
                    break
                while not budget.acquire(job.cost, self.stop):
                    if self.stop is not None and self.stop.requested:
                        break
                    if error_event.is_set():
                        break
                if self.stop is not None and self.stop.requested:
                    break
                if error_event.is_set():
                    budget.release(job.cost)
                    break
                future = executor.submit(
                    _fingerprint_worker, self.config, self.store, job
                )
                futures[future] = job
                future.add_done_callback(
                    lambda completed, submitted=job, amount=job.cost: on_complete(
                        completed, submitted, amount
                    )
                )

            for future in as_completed(list(futures)):
                if future.cancelled():
                    continue
                job = futures[future]
                try:
                    summary = future.result()
                    self.fp_written += 1
                    self.chunks_completed += 1
                    self._emit("fingerprint_chunk", summary)
                    self._checkpoint(
                        "fingerprint",
                        job.track,
                        [job.chunk.start, job.chunk.end],
                    )
                except TaskInterrupted as exc:
                    interrupted = exc
                    for pending in futures:
                        if not pending.done():
                            pending.cancel()
                    break
                except AudioFPError as exc:
                    errors_by_order.setdefault(job.order, exc)
                    for pending in futures:
                        if not pending.done():
                            pending.cancel()
                    break
                except Exception as exc:  # pragma: no cover - defensive
                    errors_by_order.setdefault(
                        job.order,
                        AudioFPError(
                            "E_TRACK_PROCESSING",
                            "并发指纹任务异常",
                            track=job.track.info.label,
                            error=str(exc),
                        ),
                    )
                    for pending in futures:
                        if not pending.done():
                            pending.cancel()
                    break
            if interrupted is None:
                try:
                    self._checkpoint("fingerprint_complete")
                except TaskInterrupted as exc:
                    interrupted = exc
        finally:
            for pending in futures:
                if not pending.done():
                    pending.cancel()
            executor.shutdown(wait=True, cancel_futures=True)

        if errors_by_order:
            raise errors_by_order[min(errors_by_order)]
        if interrupted is not None:
            raise interrupted

    def _run_candidate_phase(
        self,
        query_plans: list[TrackPlan],
        references: list[TrackSpec],
    ) -> dict[str, list[tuple[TrackSpec, int, int, str]]]:
        index = load_index(self.config.state_dir / "index.sqlite3")
        plan_by_query: dict[str, list[tuple[TrackSpec, int, int, str]]] = {}
        previous_refs = (self.previous_manifest or {}).get("reference_signatures", {})
        for query_plan in query_plans:
            query = query_plan.track
            entries: list[tuple[TrackSpec, int, int, str]] = []
            for window in _candidate_windows(self.config, query_plan):
                target_ids: set[str] = set()
                expected: dict[str, tuple[str, str]] = {}
                for reference in references:
                    expected_version = candidate_chunk_version(
                        self.config,
                        query,
                        window.dependency_version,
                        reference,
                        self.reference_versions[reference.track_id],
                        window.start,
                        window.end,
                    )
                    logical = (
                        f"cand/{query.track_id}/{reference.track_id}/"
                        f"{window.start:08d}_{window.end:08d}"
                    )
                    result = self.store.read_valid(logical, expected_version)
                    entries.append(
                        (reference, window.start, window.end, expected_version)
                    )
                    self.candidate_versions[logical] = expected_version
                    if result.status == "valid" and not self.force_full:
                        self.candidate_reused += 1
                        self.chunks_completed += 1
                        self._checkpoint(
                            "candidate_cache",
                            query,
                            [window.start, window.end],
                        )
                        continue
                    query_written = any(
                        (query.track_id, dependency_start, dependency_end)
                        in self.fresh_query_chunks
                        for dependency_start, dependency_end, _ in window.dependencies
                    )
                    reference_changed = (
                        previous_refs.get(reference.track_id)
                        != self.reference_versions[reference.track_id]
                    )
                    reason = _candidate_reason(
                        self.config,
                        self.previous_manifest,
                        status=result.status,
                        query_track_id=query.track_id,
                        query_chunk_written=query_written,
                        reference_version_changed=reference_changed,
                        force_full=self.force_full,
                        was_interrupted=self.interruption is not None,
                    )
                    self._record_affected(
                        "candidate", query, window.start, window.end, reason
                    )
                    target_ids.add(reference.track_id)
                    expected[reference.track_id] = (logical, expected_version)

                if target_ids:
                    payloads = build_candidate_payloads(
                        self.config,
                        self.store,
                        query,
                        (window.start, window.end, window.dependency_version),
                        list(window.dependencies),
                        references,
                        self.reference_versions,
                        index,
                        target_ids,
                    )
                    for reference in references:
                        if reference.track_id not in target_ids:
                            continue
                        logical, expected_version = expected[reference.track_id]
                        payload = payloads[reference.track_id]
                        self.store.write(logical, expected_version, payload)
                        self.candidate_written += 1
                        self.chunks_completed += 1
                        self._emit(
                            "candidate_chunk",
                            {
                                "query": query.info.label,
                                "reference": reference.info.label,
                                "frame_interval": [window.start, window.end],
                                "vote_count": len(payload["votes"]),
                            },
                        )
                        self._checkpoint(
                            "candidate",
                            query,
                            [window.start, window.end],
                        )
                else:
                    self._checkpoint(
                        "candidate_complete",
                        query,
                        [window.start, window.end],
                    )
            plan_by_query[query.track_id] = entries
        self._checkpoint("candidate_complete")
        return plan_by_query

    def _load_candidate_payloads(
        self,
        query: TrackSpec,
        entries: list[tuple[TrackSpec, int, int, str]],
    ) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for reference, start, end, version in entries:
            logical = (
                f"cand/{query.track_id}/{reference.track_id}/"
                f"{start:08d}_{end:08d}"
            )
            result = self.store.read_valid(logical, version)
            if result.status != "valid":
                fail(
                    "E_CACHE_READ",
                    "读取候选分块失败",
                    query=query.info.label,
                    reference=reference.info.label,
                    frame_interval=[start, end],
                    status=result.status,
                    reason=result.reason,
                )
            payloads.append(result.payload)
        return payloads

    def _write_trace(self, candidate: dict[str, Any]) -> None:
        trace = candidate.pop("score_trace", [])
        trace_name = sha256_value(
            {
                "candidate_id": candidate["candidate_id"],
                "fixed_score_units": candidate["fixed_score_units"],
                "match_count": candidate["match_count"],
            }
        )[:32]
        trace_path = self.config.state_dir / "traces" / f"{trace_name}.json"
        atomic_write_json(
            trace_path,
            {
                "schema_version": 1,
                "candidate_id": candidate["candidate_id"],
                "fixed_scale": self.config.search.fixed_scale,
                "score_tolerance": self.config.search.score_tolerance,
                "first_tolerance_exceeded_at": candidate[
                    "first_tolerance_exceeded_at"
                ],
                "match_count": candidate["match_count"],
                "trace": trace,
            },
        )
        try:
            display = trace_path.relative_to(self.config.task_dir).as_posix()
        except ValueError:
            display = trace_path.as_posix()
        candidate["score_trace_file"] = display
        candidate["score_trace_match_count"] = len(trace)
        self.trace_paths[candidate["candidate_id"]] = display

    def _aggregate(
        self,
        query_plans: list[TrackPlan],
        candidate_plan: dict[str, list[tuple[TrackSpec, int, int, str]]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        all_candidates: list[dict[str, Any]] = []
        accepted: list[dict[str, Any]] = []
        query_diagnostics: list[dict[str, Any]] = []
        for query_plan in query_plans:
            query = query_plan.track
            payloads = self._load_candidate_payloads(
                query, candidate_plan[query.track_id]
            )
            candidates, diagnostics = aggregate_query_candidates(
                self.config, query, payloads
            )
            for candidate in candidates:
                self._write_trace(candidate)
            all_candidates.extend(candidates)
            accepted.extend(
                candidate for candidate in candidates if not candidate["suppressed"]
            )
            query_diagnostics.append(
                {
                    "query_file": query.info.label,
                    "query_track_id": query.track_id,
                    "candidate_count_before_resolution": diagnostics[
                        "candidate_count_before_resolution"
                    ],
                    "candidate_count_after_resolution": diagnostics[
                        "candidate_count_after_resolution"
                    ],
                    "vote_count": diagnostics["vote_count"],
                    "histogram_modes": diagnostics["histogram_modes"],
                    "rank_mismatches": diagnostics["rank_mismatches"],
                    "unmatched": diagnostics.get("unmatched"),
                }
            )

        accepted.sort(
            key=lambda candidate: (
                -candidate["fixed_score_units"],
                -candidate["match_count"],
                candidate["query_order"],
                candidate["query_start_sample"],
                candidate["reference_order"],
                candidate["reference_start_sample"],
            )
        )
        for rank, candidate in enumerate(accepted, start=1):
            candidate["global_rank"] = rank
        return all_candidates, accepted, query_diagnostics

    def _build_report(
        self,
        plans: list[TrackPlan],
        all_candidates: list[dict[str, Any]],
        accepted: list[dict[str, Any]],
        query_diagnostics: list[dict[str, Any]],
    ) -> dict[str, Any]:
        rank_mismatches = [
            {
                "candidate_id": candidate["candidate_id"],
                "fixed_rank": candidate["fixed_rank"],
                "reference_float_rank": candidate["reference_float_rank"],
                "fixed_score": candidate["fixed_score"],
                "reference_float_score": candidate["reference_float_score"],
            }
            for candidate in all_candidates
            if candidate["fixed_float_rank_mismatch"]
        ]
        unmatched = [
            {
                "query_file": item["query_file"],
                "code": item["unmatched"]["code"],
                "message": item["unmatched"]["message"],
                "vote_count": item["unmatched"]["vote_count"],
            }
            for item in query_diagnostics
            if item.get("unmatched") is not None
        ]
        ranking = [
            {
                "rank": candidate["global_rank"],
                "candidate_id": candidate["candidate_id"],
                "query_file": candidate["query_file"],
                "reference_file": candidate["reference_file"],
                "fixed_score_units": candidate["fixed_score_units"],
                "fixed_score": candidate["fixed_score"],
                "reference_float_score": candidate["reference_float_score"],
                "match_count": candidate["match_count"],
                "query_start_sample": candidate["query_start_sample"],
                "query_end_sample": candidate["query_end_sample"],
                "reference_start_sample": candidate["reference_start_sample"],
                "reference_end_sample": candidate["reference_end_sample"],
                "integer_frame_offset": candidate["integer_frame_offset"],
                "float_reference_offset_samples": candidate[
                    "float_reference_offset_samples"
                ],
            }
            for candidate in accepted
        ]
        fp_chunk_count = sum(len(plan.chunks) for plan in plans)
        return {
            "schema_version": 1,
            "ok": True,
            "task_file": str(self.config.task_path),
            "parameters": {
                "fingerprint": self.config.fingerprint.canonical(),
                "search": self.config.search.canonical(),
                "concurrency_limit": self.config.concurrency_limit,
                "memory_budget_bytes": self.config.memory_budget_bytes,
                "chunk_frames": self.config.chunk_frames,
            },
            "summary": {
                "reference_count": len(self.config.references),
                "query_count": len(self.config.queries),
                "fingerprint_chunk_count": fp_chunk_count,
                "candidate_count_before_resolution": len(all_candidates),
                "segment_count": len(accepted),
                "suppressed_candidate_count": len(all_candidates) - len(accepted),
                "unmatched_query_count": len(unmatched),
                "fixed_float_rank_mismatch_count": len(rank_mismatches),
            },
            "judgement": {
                "fixed_point_only": True,
                "minimum_match_rule": (
                    "match_count >= min_matches AND "
                    "fixed_score_units >= min_matches * fixed_scale"
                ),
                "relative_score_floor": {
                    "value": self.config.search.relative_score_floor,
                    "basis": "fixed_score_units",
                },
                "float64_reference_path": "diagnostic_only",
            },
            "segments": accepted,
            "ranking": ranking,
            "unmatched_queries": unmatched,
            "fixed_float_rank_mismatches": rank_mismatches,
            "candidates": all_candidates,
            "query_diagnostics": query_diagnostics,
            "incremental": {
                "affected_intervals": self.affected,
                "fingerprint_chunks_written": self.fp_written,
                "fingerprint_chunks_reused": self.fp_reused,
                "candidate_chunks_written": self.candidate_written,
                "candidate_chunks_reused": self.candidate_reused,
                "resumed_from_interruption": self.interruption,
                "index": self.index_info,
            },
            "files": {
                "report": str(self.config.report_path),
                "segments": str(self.config.segments_path),
                "state_dir": str(self.config.state_dir),
            },
        }

    def _write_outputs(
        self,
        report: dict[str, Any],
        accepted: list[dict[str, Any]],
        unmatched: list[dict[str, Any]],
    ) -> None:
        atomic_write_json(self.config.report_path, report)
        atomic_write_json(
            self.config.segments_path,
            {
                "schema_version": 1,
                "segments": accepted,
                "ranking": report["ranking"],
                "unmatched_queries": unmatched,
                "fixed_point_only": True,
            },
        )

    def _write_manifest(self) -> None:
        inputs = {
            track.track_id: _track_input_manifest(track)
            for track in self.config.all_tracks
        }
        self.store.write_manifest(
            {
                "schema_version": 1,
                "fingerprint": self.config.fingerprint.canonical(),
                "search": self.config.search.canonical(),
                "inputs": inputs,
                "fingerprint_chunks": {
                    f"fp/{track_id}/{start:08d}_{end:08d}": version
                    for track_id, chunks in self.fp_versions.items()
                    for (start, end), version in chunks.items()
                },
                "candidate_chunks": dict(self.candidate_versions),
                "reference_signatures": dict(self.reference_versions),
                "index_version": self.index_info.get("index_version"),
            }
        )

    def run(self) -> dict[str, Any]:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        self._checkpoint("startup")
        reference_plans = [_frame_chunks(self.config, track) for track in self.config.references]
        query_plans = [_frame_chunks(self.config, track) for track in self.config.queries]
        all_plans = reference_plans + query_plans
        self._run_fingerprint_phase(all_plans)

        for reference_plan in reference_plans:
            track = reference_plan.track
            chunk_versions = [
                (chunk.start, chunk.end, chunk.version)
                for chunk in reference_plan.chunks
            ]
            self.reference_versions[track.track_id] = reference_signature(
                track, chunk_versions
            )
        ref_chunks = {
            plan.track.track_id: [
                (chunk.start, chunk.end, chunk.version) for chunk in plan.chunks
            ]
            for plan in reference_plans
        }
        self.index_info = rebuild_index(
            self.config,
            self.store,
            ref_chunks,
            self.reference_versions,
        )
        self._checkpoint("index")

        candidate_plan = self._run_candidate_phase(
            query_plans, list(self.config.references)
        )
        self._checkpoint("aggregation")
        all_candidates, accepted, query_diagnostics = self._aggregate(
            query_plans, candidate_plan
        )
        self._checkpoint("pre_report")
        report = self._build_report(
            all_plans, all_candidates, accepted, query_diagnostics
        )
        self._write_outputs(
            report, accepted, report["unmatched_queries"]
        )
        self._write_manifest()
        self.store.clear_interruption()
        return report


def run_search(
    task_path: Path,
    *,
    force_full: bool = False,
    stop: StopController | None = None,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    config = load_task(task_path)
    runner = SearchRunner(
        config,
        force_full=force_full,
        stop=stop,
        progress=progress,
    )
    return runner.run()

