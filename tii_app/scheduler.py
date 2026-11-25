"""Injected logical clock and deterministic time-window batching."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import Task


class LogicalClock:
    def __init__(self, start_ms: int = 0, window_ms: int = 1000) -> None:
        self._now_ms = int(start_ms)
        self.window_ms = int(window_ms)

    @property
    def now_ms(self) -> int:
        return self._now_ms

    @property
    def window_index(self) -> int:
        return self._now_ms // self.window_ms

    def advance_to(self, target_ms: int) -> int:
        target_ms = int(target_ms)
        if target_ms < self._now_ms:
            raise ValueError(
                f"logical clock cannot move backwards: {target_ms} < {self._now_ms}"
            )
        self._now_ms = target_ms
        return self._now_ms


@dataclass(frozen=True)
class TaskBatch:
    index: int
    tasks: tuple[Task, ...]
    logical_window_start: int
    logical_window_end: int


def order_tasks(tasks: Iterable[Task]) -> list[Task]:
    # Logical time is the primary scheduler key. Compute priority is an explicit
    # deterministic tie-breaker, then case_id makes equal-priority ties stable.
    return sorted(
        tasks,
        key=lambda task: (task.logical_clock_ms, -task.priority, task.case_id),
    )


def build_batches(
    tasks: Iterable[Task],
    *,
    max_tasks: int,
    window_ms: int,
) -> list[TaskBatch]:
    if max_tasks <= 0 or window_ms <= 0:
        raise ValueError("max_tasks and window_ms must be positive")
    ordered = order_tasks(tasks)
    batches: list[TaskBatch] = []
    current: list[Task] = []
    current_window: int | None = None
    batch_index = 0
    for task in ordered:
        window = task.logical_clock_ms // window_ms
        if current and (len(current) >= max_tasks or window != current_window):
            batches.append(
                TaskBatch(
                    index=batch_index,
                    tasks=tuple(current),
                    logical_window_start=int(current_window) * window_ms,
                    logical_window_end=(int(current_window) + 1) * window_ms - 1,
                )
            )
            batch_index += 1
            current = []
        if not current:
            current_window = window
        current.append(task)
    if current:
        batches.append(
            TaskBatch(
                index=batch_index,
                tasks=tuple(current),
                logical_window_start=int(current_window) * window_ms,
                logical_window_end=(int(current_window) + 1) * window_ms - 1,
            )
        )
    return batches
