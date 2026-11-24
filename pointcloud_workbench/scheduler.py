"""Deterministic, bounded, memory-aware block scheduler."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Protocol, TypeVar

from .errors import BusinessError


class LogicalClock(Protocol):
    def now(self) -> int:
        ...

    def advance(self, units: int) -> None:
        ...

    def yield_round(self) -> None:
        ...


class DeterministicClock:
    """A reproducible logical clock used by default."""

    def __init__(self) -> None:
        self.units = 0
        self.yields = 0

    def advance(self, units: int) -> None:
        self.units += int(units)

    def now(self) -> int:
        return self.units

    def yield_round(self) -> None:
        self.yields += 1

    def snapshot(self) -> dict[str, int]:
        return {"units": self.units, "yields": self.yields}


PayloadT = TypeVar("PayloadT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class SchedulerJob(Generic[PayloadT]):
    index: int
    payload: PayloadT
    sample_bytes: int
    logical_cost: int


@dataclass
class SchedulerResult(Generic[ResultT]):
    completed: list[tuple[int, ResultT]] = field(default_factory=list)
    errors: list[BusinessError] = field(default_factory=list)
    pending_indices: list[int] = field(default_factory=list)
    interrupted: bool = False
    admission_sequence: list[list[int]] = field(default_factory=list)
    peak_in_flight_bytes: int = 0
    rounds: list[dict[str, Any]] = field(default_factory=list)
    clock_snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def completed_indices(self) -> list[int]:
        return [index for index, _ in self.completed]


def _as_business_error(error: BaseException, job: SchedulerJob[Any]) -> BusinessError:
    if isinstance(error, BusinessError):
        return error
    return BusinessError(
        code="E_RENDER",
        message="block worker raised an unexpected error",
        details={
            "block_index": job.index,
            "exception_type": type(error).__name__,
            "exception": str(error),
        },
    )


def run_scheduler(
    jobs: list[SchedulerJob[PayloadT]],
    worker: Callable[[SchedulerJob[PayloadT]], ResultT],
    *,
    max_workers: int,
    memory_budget_bytes: int,
    round_budget_units: float,
    clock: LogicalClock | None = None,
    stop_event: threading.Event | None = None,
) -> SchedulerResult[ResultT]:
    """Run jobs in deterministic waves.

    A wave is admitted strictly by block index. It never exceeds the worker cap
    or memory budget. Every admitted wave is drained before the next admission,
    so thread completion order never changes the admission sequence.
    """

    if max_workers <= 0:
        raise BusinessError("E_BUDGET", "max_workers must be positive")
    if memory_budget_bytes <= 0:
        raise BusinessError("E_BUDGET", "memory budget must be positive")
    if round_budget_units <= 0:
        raise BusinessError("E_BUDGET", "round budget must be positive")

    ordered = sorted(jobs, key=lambda job: job.index)
    if len({job.index for job in ordered}) != len(ordered):
        raise BusinessError("E_SCHEDULER", "job indices must be unique")
    for job in ordered:
        if job.sample_bytes <= 0:
            raise BusinessError(
                "E_BUDGET",
                "job has a non-positive sample buffer size",
                details={"block_index": job.index, "sample_bytes": job.sample_bytes},
            )
        if job.sample_bytes > memory_budget_bytes:
            raise BusinessError(
                "E_BUDGET",
                "one block's sampling buffer exceeds the memory budget",
                details={
                    "block_index": job.index,
                    "sample_bytes": job.sample_bytes,
                    "memory_budget_bytes": memory_budget_bytes,
                },
            )

    clock = clock or DeterministicClock()
    queue = list(ordered)
    result: SchedulerResult[ResultT] = SchedulerResult()
    current_round: dict[str, Any] = {
        "index": 0,
        "waves": [],
        "elapsed_units": 0,
        "yielded": False,
        "start_units": clock.now(),
    }
    executor = ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="pcwb-block",
    )

    def finish_round(*, yielded: bool) -> None:
        if not current_round["waves"]:
            return
        current_round["yielded"] = yielded
        result.rounds.append(
            {
                "index": int(current_round["index"]),
                "waves": [list(wave) for wave in current_round["waves"]],
                "elapsed_units": int(current_round["elapsed_units"]),
                "yielded": yielded,
            }
        )
        current_round["index"] = int(current_round["index"]) + 1
        current_round["waves"] = []
        current_round["elapsed_units"] = 0
        current_round["yielded"] = False
        current_round["start_units"] = clock.now()

    try:
        while queue:
            if stop_event is not None and stop_event.is_set():
                result.interrupted = True
                result.pending_indices = [job.index for job in queue]
                break
            wave: list[SchedulerJob[PayloadT]] = []
            wave_bytes = 0
            while queue and len(wave) < max_workers:
                next_job = queue[0]
                if wave_bytes + next_job.sample_bytes > memory_budget_bytes:
                    break
                queue.pop(0)
                wave.append(next_job)
                wave_bytes += next_job.sample_bytes
            if not wave:
                raise BusinessError(
                    "E_BUDGET",
                    "scheduler cannot admit the next block without exceeding the memory budget",
                    details={
                        "block_index": queue[0].index,
                        "sample_bytes": queue[0].sample_bytes,
                        "memory_budget_bytes": memory_budget_bytes,
                    },
                )
            result.admission_sequence.append([job.index for job in wave])
            current_round["waves"].append([job.index for job in wave])
            result.peak_in_flight_bytes = max(result.peak_in_flight_bytes, wave_bytes)
            futures: dict[Future[ResultT], SchedulerJob[PayloadT]] = {
                executor.submit(worker, job): job for job in wave
            }
            wait(futures.keys())
            wave_errors: list[tuple[int, BusinessError]] = []
            wave_results: list[tuple[int, ResultT]] = []
            for future, job in futures.items():
                try:
                    wave_results.append((job.index, future.result()))
                except BaseException as exc:  # Block failures are captured as business errors.
                    wave_errors.append((job.index, _as_business_error(exc, job)))
            if wave_errors:
                result.errors = [error for _, error in sorted(wave_errors, key=lambda pair: pair[0])]
                result.pending_indices = [job.index for job in queue]
                break
            result.completed.extend(sorted(wave_results, key=lambda pair: pair[0]))
            wave_cost = max(job.logical_cost for job in wave)
            clock.advance(wave_cost)
            current_round["elapsed_units"] = clock.now() - int(current_round["start_units"])
            if stop_event is not None and stop_event.is_set():
                result.interrupted = True
                result.pending_indices = [job.index for job in queue]
                break
            if queue and float(current_round["elapsed_units"]) >= round_budget_units:
                finish_round(yielded=True)
                clock.yield_round()
        if not result.interrupted and not result.errors and not queue:
            finish_round(yielded=False)
        elif result.interrupted:
            finish_round(yielded=True)
        elif result.errors:
            finish_round(yielded=False)
            if not result.pending_indices:
                result.pending_indices = [job.index for job in queue]
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    snapshot = getattr(clock, "snapshot", None)
    result.clock_snapshot = snapshot() if callable(snapshot) else {}
    return result

