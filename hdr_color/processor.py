"""Bounded concurrent chunk scheduler, checkpoints and final atomic commit."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import threading
from typing import Any
import uuid

from . import __version__
from .errors import BusinessError
from .pipeline import SharedResources, process_chunk, validate_chunk_result
from .stats import (
    aggregate_frames,
    build_windows,
    normalize_frame_stat,
)
from .storage import (
    atomic_write_json,
    ensure_writable_directory,
    read_existing_output_metadata,
    read_json_file,
    remove_tree_files,
)
from .task import TaskConfig


OUTPUT_FRAMES_SCHEMA = "hdr-color-output-frames-v1"
OUTPUT_REPORT_SCHEMA = "hdr-color-report-v1"
STATE_SCHEMA = "hdr-color-state-v1"
CHUNK_SCHEMA = "hdr-color-chunk-v1"


@dataclass
class TerminationController:
    event: threading.Event
    signum: int | None = None
    signal_name: str | None = None
    exit_code: int | None = None

    def request(self, signum: int) -> None:
        if self.signum is None:
            self.signum = signum
            self.signal_name = signal.Signals(signum).name
            self.exit_code = 130 if signum == signal.SIGINT else 143
        self.event.set()

    def report(self) -> dict[str, Any]:
        return {
            "signal": self.signal_name,
            "signum": self.signum,
            "exit_code": self.exit_code,
        }


@dataclass(frozen=True)
class ChunkRange:
    index: int
    start_frame: int
    end_frame: int


def run_processing(
    config: TaskConfig,
    termination: TerminationController,
    test_chunk_delay_seconds: float = 0.0,
) -> dict[str, Any]:
    output_dir = config.output_dir
    ensure_writable_directory(output_dir)
    existing = read_existing_output_metadata(output_dir)
    if (
        existing is not None
        and existing["version"] == config.version
        and existing["frames_count"] == config.frame_count
        and existing["parameter_digest"] == config.parameter_digest
    ):
        _ensure_complete_state_for_existing(config, existing)
        remove_tree_files(output_dir / ".hdr-color-work")
        return {
            "status": "unchanged",
            "version": config.version,
            "frames_count": config.frame_count,
            "output_frames": str(config.output_frames_path),
            "output_report": str(config.output_report_path),
            "reason": "existing output has the same version and frame count",
        }

    shared = SharedResources.create(config)
    plan = _build_memory_plan(config, shared)
    chunks = _chunk_ranges(
        config.frame_count,
        plan["effective_chunk_frames"],
    )
    work_dir = output_dir / ".hdr-color-work"
    work_dir.mkdir(parents=True, exist_ok=True)
    _prune_work_files(work_dir, config.version)

    completed: dict[int, dict[str, Any]] = _load_persisted_chunks(
        work_dir, chunks, config.version
    )
    resumed_chunks = sorted(completed)
    _write_state(
        config,
        status="running",
        phase="processing",
        chunks=chunks,
        completed=completed,
        extra={
            "effective_chunk_frames": plan["effective_chunk_frames"],
            "effective_inflight_chunks": plan["effective_inflight_chunks"],
            "memory_plan": plan,
        },
    )

    if termination.event.is_set():
        _write_state(
            config,
            status="interrupted",
            phase="before_processing",
            chunks=chunks,
            completed=completed,
            extra={
                **termination.report(),
                "resumed_chunks": resumed_chunks,
            },
        )
        return _interrupted_outcome(config, termination, completed)

    missing = [chunk for chunk in chunks if chunk.index not in completed]
    peak_inflight_bytes = 0
    peak_inflight_chunks = 0
    if missing:
        result = _run_pool(
            config=config,
            shared=shared,
            chunks=chunks,
            missing=missing,
            completed=completed,
            work_dir=work_dir,
            termination=termination,
            in_flight_limit=plan["effective_inflight_chunks"],
            per_chunk_bytes=plan["estimated_bytes_per_chunk"],
            test_chunk_delay_seconds=test_chunk_delay_seconds,
        )
        peak_inflight_bytes = result["peak_inflight_bytes"]
        peak_inflight_chunks = result["peak_inflight_chunks"]
        if result["interrupted"]:
            _write_state(
                config,
                status="interrupted",
                phase="chunk_boundary",
                chunks=chunks,
                completed=completed,
                extra={
                    **termination.report(),
                    "resumed_chunks": resumed_chunks,
                    "peak_inflight_bytes": peak_inflight_bytes,
                    "peak_inflight_chunks": peak_inflight_chunks,
                },
            )
            return _interrupted_outcome(config, termination, completed)

    if termination.event.is_set():
        _write_state(
            config,
            status="interrupted",
            phase="before_commit",
            chunks=chunks,
            completed=completed,
            extra={
                **termination.report(),
                "resumed_chunks": resumed_chunks,
                "peak_inflight_bytes": peak_inflight_bytes,
                "peak_inflight_chunks": peak_inflight_chunks,
            },
        )
        return _interrupted_outcome(config, termination, completed)

    if len(completed) != len(chunks):
        missing_indices = [
            chunk.index for chunk in chunks if chunk.index not in completed
        ]
        raise BusinessError(
            "E_CHUNK_FAILURE",
            "chunk processing ended before all chunks were persisted",
            {
                "version": config.version,
                "missing_chunk_indices": missing_indices,
            },
        )

    frames_payload, report_payload = _assemble_outputs(
        config=config,
        shared=shared,
        chunks=chunks,
        completed=completed,
        plan=plan,
        resumed_chunks=resumed_chunks,
        peak_inflight_bytes=peak_inflight_bytes,
        peak_inflight_chunks=peak_inflight_chunks,
    )
    atomic_write_json(config.output_frames_path, frames_payload)
    atomic_write_json(config.output_report_path, report_payload)
    _write_state(
        config,
        status="complete",
        phase="committed",
        chunks=chunks,
        completed=completed,
        extra={
            "resumed_chunks": resumed_chunks,
            "peak_inflight_bytes": peak_inflight_bytes,
            "peak_inflight_chunks": peak_inflight_chunks,
            "output_frames": str(config.output_frames_path),
            "output_report": str(config.output_report_path),
        },
    )
    remove_tree_files(work_dir)
    return {
        "status": "complete",
        "version": config.version,
        "frames_count": config.frame_count,
        "valid_frames": report_payload["statistics"]["overall"]["valid_frame_count"],
        "invalid_frames": report_payload["statistics"]["overall"]["invalid_frame_count"],
        "first_injection_error_frame": report_payload["injections"][
            "first_error_frame"
        ],
        "output_frames": str(config.output_frames_path),
        "output_report": str(config.output_report_path),
        "concurrency_requested": config.concurrency,
        "effective_inflight_chunks": plan["effective_inflight_chunks"],
    }


def _run_pool(
    config: TaskConfig,
    shared: SharedResources,
    chunks: list[ChunkRange],
    missing: list[ChunkRange],
    completed: dict[int, dict[str, Any]],
    work_dir: Path,
    termination: TerminationController,
    in_flight_limit: int,
    per_chunk_bytes: int,
    test_chunk_delay_seconds: float,
) -> dict[str, Any]:
    futures: dict[Future[dict[str, Any]], ChunkRange] = {}
    next_job = 0
    in_flight_bytes = 0
    peak_inflight_bytes = 0
    peak_inflight_chunks = 0
    errors: list[tuple[int, BusinessError]] = []
    executor = ThreadPoolExecutor(
        max_workers=config.concurrency,
        thread_name_prefix="hdr-color",
    )

    def submit_ready() -> None:
        nonlocal next_job, in_flight_bytes, peak_inflight_bytes, peak_inflight_chunks
        while (
            next_job < len(missing)
            and len(futures) < in_flight_limit
            and not termination.event.is_set()
        ):
            chunk = missing[next_job]
            future = executor.submit(
                process_chunk,
                shared,
                chunk.index,
                chunk.start_frame,
                chunk.end_frame,
                test_chunk_delay_seconds,
            )
            futures[future] = chunk
            next_job += 1
            in_flight_bytes += per_chunk_bytes
            peak_inflight_bytes = max(peak_inflight_bytes, in_flight_bytes)
            peak_inflight_chunks = max(peak_inflight_chunks, len(futures))

    try:
        submit_ready()
        while futures and not termination.event.is_set() and not errors:
            done, _pending = wait(
                tuple(futures), timeout=0.05, return_when=FIRST_COMPLETED
            )
            for future in done:
                chunk = futures.pop(future)
                in_flight_bytes -= per_chunk_bytes
                if future.cancelled():
                    continue
                try:
                    result = future.result()
                    validate_chunk_result(
                        result, chunk.start_frame, chunk.end_frame
                    )
                    _persist_chunk(work_dir, config.version, chunk, result)
                    completed[chunk.index] = result
                    _write_checkpoint(config, work_dir, chunks, completed)
                except BusinessError as exc:
                    errors.append((chunk.index, exc))
                except Exception as exc:
                    errors.append(
                        (
                            chunk.index,
                            BusinessError(
                                "E_CHUNK_FAILURE",
                                "chunk worker failed",
                                {
                                    "chunk_index": chunk.index,
                                    "start_frame": chunk.start_frame,
                                    "end_frame_exclusive": chunk.end_frame,
                                    "error_type": type(exc).__name__,
                                    "error": str(exc),
                                },
                            ),
                        )
                    )
            if not errors:
                submit_ready()
    finally:
        interrupt_requested = termination.event.is_set()
        executor.shutdown(wait=True, cancel_futures=interrupt_requested or bool(errors))
        # Fully completed chunks reached a block boundary before shutdown; they
        # are safe to persist and make the checkpoint recoverable.
        for future, chunk in list(futures.items()):
            if future.cancelled() or not future.done():
                continue
            try:
                result = future.result()
                validate_chunk_result(result, chunk.start_frame, chunk.end_frame)
                _persist_chunk(work_dir, config.version, chunk, result)
                completed[chunk.index] = result
            except Exception as exc:
                if isinstance(exc, BusinessError):
                    errors.append((chunk.index, exc))
                else:
                    errors.append(
                        (
                            chunk.index,
                            BusinessError(
                                "E_CHUNK_FAILURE",
                                "chunk worker failed during shutdown",
                                {
                                    "chunk_index": chunk.index,
                                    "error_type": type(exc).__name__,
                                    "error": str(exc),
                                },
                            ),
                        )
                    )
            futures.pop(future, None)
        _write_checkpoint(config, work_dir, chunks, completed)

    if errors and not termination.event.is_set():
        errors.sort(key=lambda item: item[0])
        first_index, first_error = errors[0]
        first_error.details.setdefault("first_failed_chunk_index", first_index)
        _write_state(
            config,
            status="failed",
            phase="chunk_processing",
            chunks=chunks,
            completed=completed,
            extra={
                "error": {
                    "code": first_error.code,
                    "message": first_error.message,
                    "details": first_error.details,
                },
                "failed_chunk_indices": sorted(
                    index for index, _error in errors
                ),
            },
        )
        raise first_error

    return {
        "interrupted": termination.event.is_set(),
        "peak_inflight_bytes": peak_inflight_bytes,
        "peak_inflight_chunks": peak_inflight_chunks,
    }


def _build_memory_plan(
    config: TaskConfig, shared: SharedResources
) -> dict[str, int]:
    pixels = config.pixels_per_frame
    bytes_per_frame = max(4096, pixels * 768)
    available = config.memory_budget_bytes - shared.shared_bytes
    if available < bytes_per_frame:
        raise BusinessError(
            "E_MEMORY_BUDGET",
            "memory budget cannot hold one frame plus shared matrices/LUTs",
            {
                "memory_budget_bytes": config.memory_budget_bytes,
                "shared_bytes_estimate": shared.shared_bytes,
                "bytes_per_frame_estimate": bytes_per_frame,
            },
        )
    effective_chunk_frames = min(
        config.chunk_frames, max(1, available // bytes_per_frame)
    )
    estimated_bytes_per_chunk = effective_chunk_frames * bytes_per_frame
    effective_inflight = min(
        config.concurrency,
        max(1, available // estimated_bytes_per_chunk),
    )
    return {
        "memory_budget_bytes": config.memory_budget_bytes,
        "shared_bytes_estimate": shared.shared_bytes,
        "bytes_per_frame_estimate": bytes_per_frame,
        "configured_chunk_frames": config.chunk_frames,
        "effective_chunk_frames": int(effective_chunk_frames),
        "estimated_bytes_per_chunk": int(estimated_bytes_per_chunk),
        "configured_concurrency": config.concurrency,
        "effective_inflight_chunks": int(effective_inflight),
    }


def _chunk_ranges(frame_count: int, chunk_frames: int) -> list[ChunkRange]:
    return [
        ChunkRange(
            index=index,
            start_frame=start,
            end_frame=min(start + chunk_frames, frame_count),
        )
        for index, start in enumerate(range(0, frame_count, chunk_frames))
    ]


def _chunk_path(work_dir: Path, index: int) -> Path:
    return work_dir / f"chunk-{index:06d}.json"


def _persist_chunk(
    work_dir: Path,
    version: str,
    chunk: ChunkRange,
    result: dict[str, Any],
) -> None:
    payload = {
        "schema_version": CHUNK_SCHEMA,
        "status": "complete",
        "version": version,
        **result,
    }
    atomic_write_json(_chunk_path(work_dir, chunk.index), payload)


def _load_persisted_chunks(
    work_dir: Path,
    chunks: list[ChunkRange],
    version: str,
) -> dict[int, dict[str, Any]]:
    completed: dict[int, dict[str, Any]] = {}
    for chunk in chunks:
        path = _chunk_path(work_dir, chunk.index)
        if not path.is_file():
            continue
        try:
            payload = read_json_file(path)
            validate_chunk_result(
                payload, chunk.start_frame, chunk.end_frame
            )
            if (
                payload.get("schema_version") != CHUNK_SCHEMA
                or payload.get("version") != version
                or payload.get("chunk_index") != chunk.index
            ):
                continue
            entries = payload.get("frame_entries")
            stats = payload.get("frame_stats")
            if (
                not isinstance(entries, list)
                or not isinstance(stats, list)
                or len(entries) != chunk.end_frame - chunk.start_frame
                or len(stats) != len(entries)
            ):
                continue
            completed[chunk.index] = payload
        except (OSError, ValueError, json.JSONDecodeError, BusinessError):
            continue
    return completed


def _prune_work_files(work_dir: Path, version: str) -> None:
    for path in work_dir.iterdir():
        if not path.is_file():
            continue
        if path.name.endswith(".tmp"):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if not path.name.startswith("chunk-") or not path.name.endswith(".json"):
            continue
        try:
            payload = read_json_file(path)
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if payload.get("version") != version:
            try:
                path.unlink()
            except OSError:
                pass


def _write_checkpoint(
    config: TaskConfig,
    work_dir: Path,
    chunks: list[ChunkRange],
    completed: dict[int, dict[str, Any]],
) -> None:
    indices = sorted(completed)
    next_frame = 0
    for chunk in chunks:
        if chunk.index in completed:
            next_frame = chunk.end_frame
        else:
            break
    atomic_write_json(
        work_dir / "checkpoint.json",
        {
            "schema_version": "hdr-color-checkpoint-v1",
            "status": "running" if len(completed) < len(chunks) else "all_chunks_complete",
            "task_id": config.task_id,
            "version": config.version,
            "parameter_digest": config.parameter_digest,
            "frames_count": config.frame_count,
            "total_chunks": len(chunks),
            "completed_chunk_indices": indices,
            "completed_chunks": len(indices),
            "next_contiguous_frame": next_frame,
            "updated_at": _utc_now(),
        },
    )


def _assemble_outputs(
    config: TaskConfig,
    shared: SharedResources,
    chunks: list[ChunkRange],
    completed: dict[int, dict[str, Any]],
    plan: dict[str, int],
    resumed_chunks: list[int],
    peak_inflight_bytes: int,
    peak_inflight_chunks: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    frame_entries: list[dict[str, Any]] = []
    frame_stats: list[dict[str, Any]] = []
    injection_records: list[dict[str, Any]] = []
    mapping_totals = {
        "valid_pixel_count": 0,
        "pre_tone_luminance_sum": 0.0,
        "post_tone_luminance_sum": 0.0,
        "black_pixel_count": 0,
        "out_of_gamut_pixel_count": 0,
        "gamut_desaturated_pixel_count": 0,
        "channel_clipped_pixel_count": 0,
        "encoded_channel_clip_count": 0,
        "source_roundtrip_skipped_pixel_count": 0,
    }
    for chunk in chunks:
        payload = completed[chunk.index]
        frame_entries.extend(payload["frame_entries"])
        frame_stats.extend(payload["frame_stats"])
        injection_records.extend(payload.get("injection_records", []))
        chunk_totals = payload.get("mapping_totals", {})
        for key in mapping_totals:
            mapping_totals[key] += chunk_totals.get(key, 0.0)
    _validate_assembly(config, frame_entries, frame_stats)

    generation_id = uuid.uuid4().hex
    base_metadata = {
        "schema_version": OUTPUT_FRAMES_SCHEMA,
        "status": "complete",
        "generation_id": generation_id,
        "engine_version": __version__,
        "task_id": config.task_id,
        "version": config.version,
        "parameter_digest": config.parameter_digest,
        "frames_digest": config.frames_digest,
        "frames_count": config.frame_count,
        "pixels_per_frame": config.pixels_per_frame,
        "linear_light_units": "cd/m2 absolute target linear light",
        "encoded_units": "normalized target transfer signal in [0,1]",
    }
    frames_payload = {
        **base_metadata,
        "source_transfer": config.source_transfer,
        "target_transfer": config.target_transfer,
        "frames": frame_entries,
    }

    error_frames = sorted(
        int(item["frame_index"])
        for item in frame_stats
        if not bool(item.get("valid"))
    )
    injection_error_frames = sorted(
        {
            int(record["frame_index"])
            for record in injection_records
            if record.get("error_code")
        }
    )
    first_error_frame = min(error_frames) if error_frames else None
    valid_pixels = int(mapping_totals["valid_pixel_count"])
    pre_sum = float(mapping_totals["pre_tone_luminance_sum"])
    post_sum = float(mapping_totals["post_tone_luminance_sum"])
    if pre_sum > 0.0:
        retention = post_sum / pre_sum
        conservation = max(0.0, 1.0 - abs(post_sum - pre_sum) / pre_sum)
    else:
        retention = 1.0 if post_sum == 0.0 else 0.0
        conservation = retention
    mapping_summary = {
        "valid_pixel_count": valid_pixels,
        "pre_tone_luminance_sum_nits": pre_sum,
        "post_tone_luminance_sum_nits": post_sum,
        "luminance_retention_ratio": retention,
        "mapping_luminance_conservation": conservation,
        "black_pixel_count": int(mapping_totals["black_pixel_count"]),
        "out_of_gamut_pixel_ratio": (
            float(mapping_totals["out_of_gamut_pixel_count"]) / valid_pixels
            if valid_pixels
            else 0.0
        ),
        "gamut_desaturated_pixel_ratio": (
            float(mapping_totals["gamut_desaturated_pixel_count"]) / valid_pixels
            if valid_pixels
            else 0.0
        ),
        "channel_clipped_pixel_ratio": (
            float(mapping_totals["channel_clipped_pixel_count"]) / valid_pixels
            if valid_pixels
            else 0.0
        ),
        "encoded_signal_clipped_channel_count": int(
            mapping_totals["encoded_channel_clip_count"]
        ),
        "source_roundtrip_skipped_pixel_count": int(
            mapping_totals["source_roundtrip_skipped_pixel_count"]
        ),
    }
    normalized_stats = [normalize_frame_stat(item) for item in frame_stats]
    overall = aggregate_frames(frame_stats)
    windows = build_windows(
        frame_stats,
        config.frame_count,
        config.window_frames,
        config.tail_mode,
    )
    injections_summary = {
        "declared_count": len(config.injections),
        "applied_count": len(injection_records),
        "first_error_frame": first_error_frame,
        "error_frame_indices": injection_error_frames,
        "source_files_modified": False,
        "records": injection_records,
    }
    report_payload = {
        "schema_version": OUTPUT_REPORT_SCHEMA,
        "status": "complete",
        "generation_id": generation_id,
        "engine_version": __version__,
        "task_id": config.task_id,
        "version": config.version,
        "parameter_digest": config.parameter_digest,
        "frames_digest": config.frames_digest,
        "frames_count": config.frame_count,
        "pixels_per_frame": config.pixels_per_frame,
        "statistics_source": "final converted target linear-light frames only",
        "parameter_summary": config.parameter_summary,
        "curves_and_matrices": shared.report(),
        "statistics": {
            "overall": overall,
            "window_config": {
                "window_frames": config.window_frames,
                "tail_mode": config.tail_mode,
                "range_convention": "[start_frame, end_frame_exclusive)",
            },
            "windows": windows,
            "frames": normalized_stats,
        },
        "mapping_summary": mapping_summary,
        "injections": injections_summary,
        "processing": {
            "requested_concurrency": config.concurrency,
            "effective_inflight_chunks": plan["effective_inflight_chunks"],
            "peak_inflight_chunks": peak_inflight_chunks,
            "peak_inflight_bytes_estimate": peak_inflight_bytes,
            "memory_plan": plan,
            "resumed_from_persisted_chunks": resumed_chunks,
            "deterministic_chunk_order": True,
            "shared_curve_luts_and_matrices": True,
        },
        "output": {
            "frames_file": config.output_frames_path.name,
            "report_file": config.output_report_path.name,
            "atomic_same_directory_replace": True,
        },
    }
    return frames_payload, report_payload


def _validate_assembly(
    config: TaskConfig,
    frame_entries: list[dict[str, Any]],
    frame_stats: list[dict[str, Any]],
) -> None:
    if len(frame_entries) != config.frame_count or len(frame_stats) != config.frame_count:
        raise BusinessError(
            "E_ASSEMBLY",
            "assembled frame count does not match the task",
            {
                "expected": config.frame_count,
                "frame_entries": len(frame_entries),
                "frame_stats": len(frame_stats),
            },
        )
    for expected, entry, stats in zip(
        range(config.frame_count), frame_entries, frame_stats
    ):
        if entry.get("frame_index") != expected or stats.get("frame_index") != expected:
            raise BusinessError(
                "E_ASSEMBLY",
                "assembled frame order is not deterministic",
                {
                    "expected_frame_index": expected,
                    "entry_frame_index": entry.get("frame_index"),
                    "stats_frame_index": stats.get("frame_index"),
                },
            )


def _write_state(
    config: TaskConfig,
    status: str,
    phase: str,
    chunks: list[ChunkRange],
    completed: dict[int, dict[str, Any]],
    extra: dict[str, Any],
) -> None:
    indices = sorted(completed)
    next_frame = 0
    for chunk in chunks:
        if chunk.index in completed:
            next_frame = chunk.end_frame
        else:
            break
    payload = {
        "schema_version": STATE_SCHEMA,
        "status": status,
        "phase": phase,
        "engine_version": __version__,
        "task_id": config.task_id,
        "version": config.version,
        "parameter_digest": config.parameter_digest,
        "frames_count": config.frame_count,
        "total_chunks": len(chunks),
        "completed_chunks": len(indices),
        "completed_chunk_indices": indices,
        "next_contiguous_frame": next_frame,
        "updated_at": _utc_now(),
        **extra,
    }
    atomic_write_json(config.state_path, payload)


def _interrupted_outcome(
    config: TaskConfig,
    termination: TerminationController,
    completed: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": "interrupted",
        "task_id": config.task_id,
        "version": config.version,
        "frames_count": config.frame_count,
        "completed_chunks": len(completed),
        "state_file": str(config.state_path),
        **termination.report(),
    }


def _ensure_complete_state_for_existing(
    config: TaskConfig, existing: dict[str, Any]
) -> None:
    current: dict[str, Any] | None = None
    if config.state_path.is_file():
        try:
            current = read_json_file(config.state_path)
        except (OSError, ValueError, json.JSONDecodeError):
            current = None
    if (
        isinstance(current, dict)
        and current.get("status") == "complete"
        and current.get("version") == config.version
    ):
        return
    atomic_write_json(
        config.state_path,
        {
            "schema_version": STATE_SCHEMA,
            "status": "complete",
            "phase": "recovered_complete_output",
            "engine_version": __version__,
            "task_id": config.task_id,
            "version": config.version,
            "parameter_digest": config.parameter_digest,
            "frames_count": config.frame_count,
            "total_chunks": None,
            "completed_chunks": None,
            "completed_chunk_indices": [],
            "next_contiguous_frame": config.frame_count,
            "generation_id": existing.get("generation_id"),
            "updated_at": _utc_now(),
        },
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
