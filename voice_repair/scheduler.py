from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .errors import BusinessIssue, RepairError, UnschedulableError
from .runtime import RecordingRuntime
from .task import RecordingSpec, Task


STAGE_LOGICAL_NS = (4, 12, 24, 15)
BLOCK_BASE_NS = 2_000


def logical_cost_ns(recording: RecordingSpec, stage: int, sample_count: int) -> int:
    if stage == 0:
        return BLOCK_BASE_NS + sample_count * STAGE_LOGICAL_NS[0]
    if stage == 1:
        return (
            BLOCK_BASE_NS
            + sample_count * STAGE_LOGICAL_NS[1]
            + recording.declick.radius * 17
        )
    if stage == 2:
        return (
            BLOCK_BASE_NS
            + sample_count * STAGE_LOGICAL_NS[2]
            + recording.denoise.frame_size * 11
        )
    return BLOCK_BASE_NS + sample_count * STAGE_LOGICAL_NS[3]


def memory_reservation_bytes(recording: RecordingSpec, stage: int, sample_count: int) -> int:
    base = 8_192
    if stage == 0:
        return base + sample_count * 32
    if stage == 1:
        return base + sample_count * 48
    if stage == 2:
        return base + sample_count * 56 + recording.denoise.frame_size * 64
    return base + sample_count * 32


@dataclass(frozen=True)
class BlockTask:
    recording_id: str
    stage: int
    block_index: int
    start_sample: int
    end_sample: int
    memory_bytes: int
    logical_cost_ns: int

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.recording_id, self.block_index, self.stage)

    def as_dict(self) -> dict:
        return {
            "recording_id": self.recording_id,
            "stage": self.stage,
            "block_index": self.block_index,
            "sample_range": [self.start_sample, self.end_sample],
            "memory_bytes": self.memory_bytes,
            "logical_cost_ns": self.logical_cost_ns,
        }


@dataclass
class RoundRecord:
    round_number: int
    logical_cost_ns: int
    admitted: list[dict] = field(default_factory=list)
    deferred: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "round": self.round_number,
            "logical_cost_ns": self.logical_cost_ns,
            "admitted": self.admitted,
            "deferred": self.deferred,
        }


@dataclass
class ScheduleResult:
    rounds: list[RoundRecord]
    interrupted: bool
    signal_number: int | None
    issues: list[BusinessIssue]
    completed_tasks: int
    inserted_blocks: int
    deferred_count: int


class DeterministicLogicalClock:
    def __init__(self) -> None:
        self._value_ns = 0
        self._lock = threading.Lock()

    def now_ns(self) -> int:
        with self._lock:
            return self._value_ns

    def advance_ns(self, delta: int) -> None:
        if delta < 0:
            raise ValueError("logical clock cannot move backwards")
        with self._lock:
            self._value_ns += int(delta)


class BlockScheduler:
    def __init__(
        self,
        *,
        task: Task,
        runtimes: list[RecordingRuntime],
        workers: int,
        stop_event: threading.Event,
        signal_number: Callable[[], int | None],
        clock: DeterministicLogicalClock | None = None,
    ) -> None:
        self.task = task
        self.runtimes = {runtime.recording_id: runtime for runtime in runtimes}
        self.workers = workers
        self.stop_event = stop_event
        self.signal_number = signal_number
        self.clock = clock or DeterministicLogicalClock()
        self._pending: dict[tuple[str, int, int], BlockTask] = {}
        self._done: set[tuple[str, int, int]] = set()
        self._stage_complete = {
            runtime.recording_id: {
                stage: runtime.stage_complete(stage) for stage in range(4)
            }
            for runtime in runtimes
        }
        self._failed_records: set[str] = set()
        self._issues: list[BusinessIssue] = []
        self._build_tasks()

    def _build_tasks(self) -> None:
        for runtime in self.runtimes.values():
            for stage in range(4):
                for block_index, (start, end) in enumerate(runtime.block_ranges(stage)):
                    key = (runtime.recording_id, block_index, stage)
                    if runtime.is_completed(stage, block_index):
                        self._done.add(key)
                        continue
                    self._pending[key] = BlockTask(
                        recording_id=runtime.recording_id,
                        stage=stage,
                        block_index=block_index,
                        start_sample=start,
                        end_sample=end,
                        memory_bytes=memory_reservation_bytes(
                            runtime.spec, stage, end - start
                        ),
                        logical_cost_ns=logical_cost_ns(
                            runtime.spec, stage, end - start
                        ),
                    )

    def _ready(self, task: BlockTask) -> bool:
        if task.stage > 0 and not self._stage_complete[task.recording_id][task.stage - 1]:
            return False
        if task.block_index == 0:
            return True
        previous = (task.recording_id, task.block_index - 1, task.stage)
        return previous in self._done

    def _mark_done(self, task: BlockTask) -> None:
        self._done.add(task.key)
        runtime = self.runtimes[task.recording_id]
        if runtime.stage_complete(task.stage):
            self._stage_complete[task.recording_id][task.stage] = True

    def _record_failure(self, recording_id: str, code: str, message: str) -> None:
        if recording_id in self._failed_records:
            return
        self._failed_records.add(recording_id)
        self._issues.append(BusinessIssue(recording_id, code, message))
        for key in [key for key in self._pending if key[0] == recording_id]:
            del self._pending[key]

    def run(self) -> ScheduleResult:
        rounds: list[RoundRecord] = []
        completed_tasks = 0
        inserted_blocks = 0
        deferred_count = 0
        interrupted = False

        with ThreadPoolExecutor(
            max_workers=self.workers,
            thread_name_prefix="voice-repair",
        ) as executor:
            round_number = 0
            while self._pending:
                if self.stop_event.is_set():
                    interrupted = True
                    break
                round_number += 1
                ready = sorted(
                    (
                        task
                        for task in self._pending.values()
                        if self._ready(task)
                    ),
                    key=lambda task: task.key,
                )
                if not ready:
                    raise UnschedulableError(
                        "E_DEPENDENCY_DEADLOCK",
                        "pending blocks exist but none are ready",
                    )

                round_record = RoundRecord(round_number=round_number, logical_cost_ns=0)
                admitted: list[BlockTask] = []
                used_memory = 0
                used_time = 0
                for task in ready:
                    reason = self._admission_rejection(
                        task,
                        admitted_count=len(admitted),
                        used_memory=used_memory,
                        used_time=used_time,
                    )
                    if reason is not None:
                        deferred = task.as_dict()
                        deferred["reason"] = reason
                        round_record.deferred.append(deferred)
                        continue
                    admitted.append(task)
                    used_memory += task.memory_bytes
                    used_time += task.logical_cost_ns

                if not admitted:
                    minimum = min(ready, key=lambda task: task.key)
                    if minimum.memory_bytes > self.task.memory_budget_bytes:
                        raise UnschedulableError(
                            "E_MEMORY_BUDGET_UNSATISFIABLE",
                            f"smallest ready block reserves {minimum.memory_bytes} bytes, "
                            f"budget is {self.task.memory_budget_bytes}",
                        )
                    if minimum.logical_cost_ns > self.task.round_time_budget_ns:
                        raise UnschedulableError(
                            "E_ROUND_BUDGET_UNSATISFIABLE",
                            f"smallest ready block costs {minimum.logical_cost_ns} ns, "
                            f"round budget is {self.task.round_time_budget_ns} ns",
                        )
                    raise UnschedulableError(
                        "E_SCHEDULER_STALL",
                        "no block could be admitted despite individually feasible work",
                    )

                round_record.logical_cost_ns = used_time
                round_record.admitted = [task.as_dict() for task in admitted]
                deferred_count += len(round_record.deferred)
                rounds.append(round_record)

                futures = []
                for task in admitted:
                    runtime = self.runtimes[task.recording_id]
                    future = executor.submit(
                        runtime.compute_and_commit,
                        task.stage,
                        task.block_index,
                        task.logical_cost_ns,
                    )
                    futures.append((task, future))

                for task, future in futures:
                    try:
                        inserted = bool(future.result())
                    except Exception as exc:
                        if isinstance(exc, RepairError):
                            code = exc.code
                            message = exc.message
                        else:
                            code = "E_BLOCK_PROCESSING"
                            message = f"{type(exc).__name__}: {exc}"
                        self._record_failure(task.recording_id, code, message)
                    else:
                        completed_tasks += 1
                        inserted_blocks += 1 if inserted else 0
                        self._mark_done(task)
                    finally:
                        self._pending.pop(task.key, None)

                self.clock.advance_ns(used_time)
                if self.stop_event.is_set():
                    interrupted = True
                    break

        return ScheduleResult(
            rounds=rounds,
            interrupted=interrupted,
            signal_number=self.signal_number() if interrupted else None,
            issues=self._issues,
            completed_tasks=completed_tasks,
            inserted_blocks=inserted_blocks,
            deferred_count=deferred_count,
        )

    def _admission_rejection(
        self,
        task: BlockTask,
        *,
        admitted_count: int,
        used_memory: int,
        used_time: int,
    ) -> str | None:
        if admitted_count >= self.task.parallelism:
            return "concurrency_limit"
        if used_memory + task.memory_bytes > self.task.memory_budget_bytes:
            return "memory_budget"
        if used_time + task.logical_cost_ns > self.task.round_time_budget_ns:
            return "round_time_budget"
        return None
