"""Deterministic replica replay, persisted batches and crash recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .errors import ParameterError, StateError
from .estimator import (
    EstimatorParameters,
    ReplayParameters,
    estimate_delays,
)
from .segment import LogRecord, Segment, collect_records, record_semantic_digest
from .storage import ProgressStore


@dataclass(frozen=True)
class PlannedBatch:
    batch_id: int
    start_ms: int
    end_ms: int
    records: tuple[LogRecord, ...]

    @property
    def start_commit_seq(self) -> int:
        return self.records[0].commit_seq

    @property
    def end_commit_seq(self) -> int:
        return self.records[-1].commit_seq

    @property
    def commit_seqs(self) -> tuple[int, ...]:
        return tuple(record.commit_seq for record in self.records)


@dataclass(frozen=True)
class ReplayPlan:
    parameters: ReplayParameters
    record_count: int
    batches: tuple[PlannedBatch, ...]
    samples: tuple[dict[str, Any], ...]


BatchCallback = Callable[[PlannedBatch], None]
CrashHook = Callable[[PlannedBatch], None]


def _validate_records(records: Sequence[LogRecord]) -> None:
    previous: int | None = None
    for record in records:
        if previous is not None and record.commit_seq <= previous:
            raise StateError("records are not in strictly increasing commit order")
        previous = record.commit_seq


def build_replay_plan(
    records: Sequence[LogRecord],
    *,
    replay_rate: int,
    concurrency: int,
    clock_origin_ms: int | None = None,
) -> ReplayPlan:
    ordered = sorted(records, key=lambda record: record.commit_seq)
    _validate_records(ordered)
    known_timestamps = [
        record.commit_ts_ms for record in ordered if record.commit_ts_ms is not None
    ]
    max_timestamp = max(known_timestamps, default=0)
    origin = clock_origin_ms if clock_origin_ms is not None else max_timestamp
    parameters = ReplayParameters(
        replay_rate=replay_rate,
        concurrency=concurrency,
        clock_origin_ms=origin,
    )
    if origin < max_timestamp:
        raise ParameterError(
            "clock_origin_ms precedes a known commit timestamp; delays would be negative",
            details={"clock_origin_ms": origin, "max_commit_ts_ms": max_timestamp},
        )

    duration = parameters.batch_duration_ms
    batches: list[PlannedBatch] = []
    samples: list[dict[str, Any]] = []
    for chunk_index, start in enumerate(range(0, len(ordered), concurrency)):
        chunk = tuple(ordered[start : start + concurrency])
        batch_start = origin + chunk_index * duration
        batch_end = batch_start + duration
        batch = PlannedBatch(
            batch_id=chunk_index,
            start_ms=batch_start,
            end_ms=batch_end,
            records=chunk,
        )
        batches.append(batch)
        for record in chunk:
            if record.commit_ts_ms is None:
                delay = None
                status = "DELAY_UNKNOWN"
            else:
                delay = batch_end - record.commit_ts_ms
                status = "KNOWN"
                if delay < 0:
                    raise StateError(
                        "negative replication delay in replay plan",
                        details={
                            "commit_seq": record.commit_seq,
                            "completion_ts_ms": batch_end,
                            "commit_ts_ms": record.commit_ts_ms,
                        },
                    )
            samples.append(
                {
                    "sample_index": len(samples) + 1,
                    "commit_seq": record.commit_seq,
                    "batch_id": batch.batch_id,
                    "commit_version": record.original_format_version,
                    "commit_ts_ms": record.commit_ts_ms,
                    "completion_ts_ms": batch_end,
                    "delay_ms": delay,
                    "status": status,
                }
            )
    return ReplayPlan(
        parameters=parameters,
        record_count=len(ordered),
        batches=tuple(batches),
        samples=tuple(samples),
    )


class ReplayEngine:
    def __init__(
        self,
        store: ProgressStore,
        *,
        session_id: str = "default",
        estimator_parameters: EstimatorParameters | None = None,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.estimator_parameters = estimator_parameters or EstimatorParameters()

    def recover(self) -> dict[str, Any]:
        result = self.store.mark_incomplete_unknown(self.session_id)
        result.update(
            {
                "business_code": None,
                "status": "RECOVERED_FROM_PERSISTED_STATE",
                "claim_exit_report": False,
                "note": (
                    "The previous process was not available to produce an exit report. "
                    "Only SQLite state was used; IN_PROGRESS batches are UNKNOWN and "
                    "must be replayed. Confirmed samples are retained exactly once."
                ),
            }
        )
        return result

    def _prepare_state(
        self,
        *,
        segments: Sequence[Segment],
        replay_rate: int,
        concurrency: int,
        reset: bool,
        clock_origin_ms: int | None,
    ) -> tuple[ReplayPlan, dict[str, Any] | None, bool]:
        records = collect_records(segments)
        log_digest = record_semantic_digest(records)
        versions = sorted({version for segment in segments for version in segment.versions})
        max_timestamp = max(
            (record.commit_ts_ms for record in records if record.commit_ts_ms is not None),
            default=0,
        )
        state = self.store.get_state(self.session_id)
        initialized = False
        if reset or state is None:
            origin = clock_origin_ms if clock_origin_ms is not None else max_timestamp
            plan = build_replay_plan(
                records,
                replay_rate=replay_rate,
                concurrency=concurrency,
                clock_origin_ms=origin,
            )
            self.store.reset_session(
                session_id=self.session_id,
                params_version=plan.parameters.version,
                estimator_version=self.estimator_parameters.version,
                replay_rate=replay_rate,
                concurrency=concurrency,
                clock_origin_ms=origin,
                log_digest=log_digest,
                segment_versions=versions,
            )
            initialized = True
            state = self.store.get_state(self.session_id)
        if state is None:
            raise StateError("unable to initialize replay state")
        mismatches: list[str] = []
        if state["replay_rate"] != replay_rate:
            mismatches.append("replay_rate")
        if state["concurrency"] != concurrency:
            mismatches.append("concurrency")
        if state["log_digest"] != log_digest:
            mismatches.append("log_digest")
        if state["estimator_version"] != self.estimator_parameters.version:
            mismatches.append("estimator_version")
        if mismatches:
            raise StateError(
                "persisted replay state does not match the requested reconstruction",
                details={
                    "mismatches": mismatches,
                    "hint": "use reset=True to start a new replay session",
                },
            )
        origin = int(state["clock_origin_ms"])
        if state["segment_versions"] != versions:
            self.store.update_session_topology(
                self.session_id,
                log_digest=log_digest,
                segment_versions=versions,
            )
            state = self.store.get_state(self.session_id)
            if state is None:
                raise StateError("replay state disappeared while updating topology")
        if clock_origin_ms is not None and clock_origin_ms != origin:
            raise StateError(
                "resume cannot change clock_origin_ms",
                details={"persisted": origin, "requested": clock_origin_ms},
            )
        plan = build_replay_plan(
            records,
            replay_rate=replay_rate,
            concurrency=concurrency,
            clock_origin_ms=origin,
        )
        return plan, state, initialized

    def run(
        self,
        segments: Sequence[Segment],
        *,
        replay_rate: int,
        concurrency: int,
        reset: bool = False,
        clock_origin_ms: int | None = None,
        crash_hook: CrashHook | None = None,
        on_batch_complete: BatchCallback | None = None,
        max_new_batches: int | None = None,
    ) -> dict[str, Any]:
        if max_new_batches is not None and max_new_batches < 0:
            raise ParameterError("max_new_batches cannot be negative")
        # If a prior process died mid-batch, convert that batch to UNKNOWN before
        # resuming.  Calling recover() explicitly yields the same idempotent result.
        automatic_recovery = self.store.mark_incomplete_unknown(self.session_id)
        plan, state, initialized = self._prepare_state(
            segments=segments,
            replay_rate=replay_rate,
            concurrency=concurrency,
            reset=reset,
            clock_origin_ms=clock_origin_ms,
        )
        batches = self.store.get_batches(self.session_id)
        completed_ids = {
            int(batch["batch_id"])
            for batch in batches
            if batch["status"] == "COMPLETE"
        }
        newly_completed: list[int] = []
        for batch in plan.batches:
            if batch.batch_id in completed_ids:
                continue
            self.store.begin_batch(
                session_id=self.session_id,
                batch_id=batch.batch_id,
                start_commit_seq=batch.start_commit_seq,
                end_commit_seq=batch.end_commit_seq,
                start_ms=batch.start_ms,
                end_ms=batch.end_ms,
                commit_seqs=batch.commit_seqs,
            )
            if crash_hook is not None:
                crash_hook(batch)
            sample_by_seq = {
                sample["commit_seq"]: sample for sample in plan.samples
            }
            batch_samples = [sample_by_seq[seq] for seq in batch.commit_seqs]
            self.store.commit_batch(
                session_id=self.session_id,
                batch_id=batch.batch_id,
                samples=batch_samples,
            )
            completed_ids.add(batch.batch_id)
            newly_completed.append(batch.batch_id)
            if on_batch_complete is not None:
                on_batch_complete(batch)
            if max_new_batches is not None and len(newly_completed) >= max_new_batches:
                break

        state = self.store.get_state(self.session_id)
        if state is None:
            raise StateError("replay state disappeared after execution")
        samples = self.store.get_samples(self.session_id)
        estimate = estimate_delays(
            samples,
            parameters=self.estimator_parameters,
            replay_parameters=plan.parameters,
            incomplete_unknown_batches=int(state["unknown_batch_count"]),
        )
        return {
            "business_code": None,
            "session_id": self.session_id,
            "initialized": initialized,
            "clock_source": (
                "integer logical clock in milliseconds; master commit timestamps are "
                "assigned from the session origin, and replica completion timestamps "
                "are produced by the deterministic batch schedule"
            ),
            "observation": (
                "delay(seq) = replica_completion_ts_ms(seq) - master_commit_ts_ms(seq); "
                "missing commit timestamps yield delay=None (unknown)"
            ),
            "replay_parameters": plan.parameters.to_dict(),
            "record_count": plan.record_count,
            "batch_count": len(plan.batches),
            "newly_completed_batch_ids": newly_completed,
            "completed_batch_ids": [
                batch["batch_id"]
                for batch in self.store.get_batches(self.session_id)
                if batch["status"] == "COMPLETE"
            ],
            "unknown_batch_count": int(state["unknown_batch_count"]),
            "last_commit_seq": int(state["last_commit_seq"]),
            "sample_count": len(samples),
            "samples": samples,
            "estimate": estimate,
            "automatic_recovery": automatic_recovery,
        }
