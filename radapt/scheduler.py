from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable

from .errors import ErrorCode
from .model import RuntimeConfig


STATUS_COMPLETED = "completed_after_start"
STATUS_FAILED = "failed_after_start"
STATUS_NOT_STARTED_PARALLEL_LIMIT = "not_started_within_budget_parallel_limit"
STATUS_ABANDONED_LOGICAL = "abandoned_logical_budget"
STATUS_ABANDONED_MEMORY = "abandoned_memory_budget"


@dataclass
class ScheduledTask:
    task_id: str
    window_id: str
    logical_estimate_ms: Fraction
    memory_estimate_bytes: int
    fn: Callable[[], Any]
    batch_seq: int | None = None


@dataclass
class TaskOutcome:
    task_id: str
    window_id: str
    status: str
    started: bool
    result: Any = None
    error_code: str | None = None
    error_message: str | None = None
    logical_estimate_ms: str | None = None
    memory_estimate_bytes: int | None = None
    reason: str | None = None
    batch_seq: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "window_id": self.window_id,
            "batch_seq": self.batch_seq,
            "status": self.status,
            "started": self.started,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "logical_estimate_ms": self.logical_estimate_ms,
            "memory_estimate_bytes": self.memory_estimate_bytes,
            "reason": self.reason,
        }


@dataclass
class RoundReport:
    outcomes: list[TaskOutcome] = field(default_factory=list)
    logical_budget_ms: Fraction = Fraction(0)
    memory_budget_bytes: int = 0
    logical_reserved_ms: Fraction = Fraction(0)
    memory_reserved_bytes: int = 0
    parallel_limit: int = 1

    @property
    def completed(self) -> list[TaskOutcome]:
        return [item for item in self.outcomes if item.status == STATUS_COMPLETED]

    @property
    def failed_after_start(self) -> list[TaskOutcome]:
        return [item for item in self.outcomes if item.status == STATUS_FAILED]

    @property
    def not_started_within_budget(self) -> list[TaskOutcome]:
        return [item for item in self.outcomes if item.status == STATUS_NOT_STARTED_PARALLEL_LIMIT]

    @property
    def actively_abandoned(self) -> list[TaskOutcome]:
        return [
            item
            for item in self.outcomes
            if item.status in {STATUS_ABANDONED_LOGICAL, STATUS_ABANDONED_MEMORY}
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "parallel_limit": self.parallel_limit,
            "logical_budget_ms": str(self.logical_budget_ms),
            "memory_budget_bytes": self.memory_budget_bytes,
            "logical_reserved_ms": str(self.logical_reserved_ms),
            "memory_reserved_bytes": self.memory_reserved_bytes,
            "counts": {
                "completed_after_start": len(self.completed),
                "failed_after_start": len(self.failed_after_start),
                "not_started_within_budget": len(self.not_started_within_budget),
                "actively_abandoned": len(self.actively_abandoned),
            },
            "outcomes": [item.to_dict() for item in self.outcomes],
        }


class ParallelScheduler:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config

    def run_round(
        self,
        tasks: list[ScheduledTask],
        *,
        logical_budget_ms: Fraction | int | None = None,
        memory_budget_bytes: int | None = None,
        max_parallel_tasks: int | None = None,
    ) -> RoundReport:
        logical_budget = Fraction(
            self.config.logical_time_budget_per_round_ms if logical_budget_ms is None else logical_budget_ms
        )
        memory_budget = self.config.memory_budget_bytes if memory_budget_bytes is None else memory_budget_bytes
        parallel_limit = self.config.max_parallel_tasks if max_parallel_tasks is None else max_parallel_tasks
        if parallel_limit <= 0:
            raise ValueError("parallel_limit must be positive")

        report = RoundReport(
            logical_budget_ms=logical_budget,
            memory_budget_bytes=memory_budget,
            parallel_limit=parallel_limit,
        )
        remaining_logical = logical_budget
        remaining_memory = memory_budget
        slots = parallel_limit
        admitted: list[ScheduledTask] = []

        for index, task in enumerate(tasks):
            estimate = Fraction(task.logical_estimate_ms)
            if estimate < 0:
                report.outcomes.append(
                    TaskOutcome(
                        task.task_id,
                        task.window_id,
                        STATUS_ABANDONED_LOGICAL,
                        False,
                        error_code=ErrorCode.LOGICAL_BUDGET_INVALID,
                        reason="negative logical estimate",
                        batch_seq=task.batch_seq,
                    )
                )
                continue
            if estimate > remaining_logical:
                reason = (
                    f"logical estimate {estimate}ms exceeds remaining {remaining_logical}ms; "
                    "remaining windows actively abandoned"
                )
                for leftover in tasks[index:]:
                    report.outcomes.append(
                        TaskOutcome(
                            leftover.task_id,
                            leftover.window_id,
                            STATUS_ABANDONED_LOGICAL,
                            False,
                            logical_estimate_ms=str(leftover.logical_estimate_ms),
                            memory_estimate_bytes=leftover.memory_estimate_bytes,
                            reason=reason,
                            batch_seq=leftover.batch_seq,
                        )
                    )
                break
            if task.memory_estimate_bytes > remaining_memory:
                reason = (
                    f"memory estimate {task.memory_estimate_bytes}B exceeds remaining {remaining_memory}B; "
                    "remaining windows actively abandoned"
                )
                for leftover in tasks[index:]:
                    report.outcomes.append(
                        TaskOutcome(
                            leftover.task_id,
                            leftover.window_id,
                            STATUS_ABANDONED_MEMORY,
                            False,
                            logical_estimate_ms=str(leftover.logical_estimate_ms),
                            memory_estimate_bytes=leftover.memory_estimate_bytes,
                            reason=reason,
                            batch_seq=leftover.batch_seq,
                        )
                    )
                break
            if slots == 0:
                reason = (
                    f"all {parallel_limit} parallel slots are busy; estimate fits budgets but task is "
                    "not started in this wave"
                )
                for leftover in tasks[index:]:
                    report.outcomes.append(
                        TaskOutcome(
                            leftover.task_id,
                            leftover.window_id,
                            STATUS_NOT_STARTED_PARALLEL_LIMIT,
                            False,
                            logical_estimate_ms=str(leftover.logical_estimate_ms),
                            memory_estimate_bytes=leftover.memory_estimate_bytes,
                            reason=reason,
                            batch_seq=leftover.batch_seq,
                        )
                    )
                break

            admitted.append(task)
            slots -= 1
            remaining_logical -= estimate
            remaining_memory -= task.memory_estimate_bytes
            report.logical_reserved_ms += estimate
            report.memory_reserved_bytes += task.memory_estimate_bytes

        if admitted:
            with ThreadPoolExecutor(max_workers=parallel_limit) as executor:
                future_map = {executor.submit(task.fn): task for task in admitted}
                for future, task in future_map.items():
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001 - scheduler must classify all startup failures.
                        report.outcomes.append(
                            TaskOutcome(
                                task.task_id,
                                task.window_id,
                                STATUS_FAILED,
                                True,
                                error_code=ErrorCode.TASK_FAILED_AFTER_START,
                                error_message=str(exc),
                                logical_estimate_ms=str(task.logical_estimate_ms),
                                memory_estimate_bytes=task.memory_estimate_bytes,
                                reason="task raised after it had started; no partial commit was made",
                                batch_seq=task.batch_seq,
                            )
                        )
                    else:
                        report.outcomes.append(
                            TaskOutcome(
                                task.task_id,
                                task.window_id,
                                STATUS_COMPLETED,
                                True,
                                result=result,
                                logical_estimate_ms=str(task.logical_estimate_ms),
                                memory_estimate_bytes=task.memory_estimate_bytes,
                                batch_seq=task.batch_seq,
                            )
                        )
        report.outcomes.sort(key=lambda item: (item.batch_seq is None, item.batch_seq or 0, item.task_id))
        return report
