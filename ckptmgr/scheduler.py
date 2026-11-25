"""Deterministic logical-time scheduler for concurrent shard writes."""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple


@dataclass(frozen=True)
class ScheduledTask:
    shard_id: str
    shard_index: int
    logical_cost_ms: int
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class BudgetAbandonedTask:
    shard_id: str
    shard_index: int
    logical_cost_ms: int
    reason: str


@dataclass(frozen=True)
class Schedule:
    start_ms: int
    deadline_ms: int
    started: Tuple[ScheduledTask, ...]
    budget_unstarted: Tuple[BudgetAbandonedTask, ...]
    virtual_end_ms: int

    def to_dict(self) -> Dict[str, object]:
        return {
            "start_ms": self.start_ms,
            "deadline_ms": self.deadline_ms,
            "virtual_end_ms": self.virtual_end_ms,
            "started": [
                {
                    "shard_id": item.shard_id,
                    "shard_index": item.shard_index,
                    "logical_cost_ms": item.logical_cost_ms,
                    "start_ms": item.start_ms,
                    "end_ms": item.end_ms,
                }
                for item in self.started
            ],
            "budget_unstarted": [
                {
                    "shard_id": item.shard_id,
                    "shard_index": item.shard_index,
                    "logical_cost_ms": item.logical_cost_ms,
                    "reason": item.reason,
                }
                for item in self.budget_unstarted
            ],
        }


def schedule_round(
    tasks: Iterable[Tuple[str, int, int]],
    start_ms: int,
    budget_ms: int,
    concurrency: int,
) -> Schedule:
    """Simulate a fixed-concurrency round using only injected logical time.

    A task starts only while the virtual clock is strictly before the round
    deadline. Once started it always runs to completion, even if its end time
    crosses the deadline. Tasks whose earliest start would fall on or after the
    deadline are never started and are reported as budget-unstarted.
    """

    pending: List[Tuple[str, int, int]] = sorted(tasks, key=lambda item: item[1])
    running: List[ScheduledTask] = []
    started: List[ScheduledTask] = []
    abandoned: List[BudgetAbandonedTask] = []
    now = start_ms
    deadline = start_ms + budget_ms

    while pending or running:
        while pending and len(running) < concurrency and now < deadline:
            shard_id, shard_index, cost = pending.pop(0)
            task = ScheduledTask(
                shard_id=shard_id,
                shard_index=shard_index,
                logical_cost_ms=cost,
                start_ms=now,
                end_ms=now + cost,
            )
            running.append(task)
            started.append(task)

        if running:
            now = min(task.end_ms for task in running)
            completed = [task for task in running if task.end_ms <= now]
            running = [task for task in running if task.end_ms > now]
            if not completed:
                raise RuntimeError("logical scheduler made no progress")

        while pending and (now >= deadline or not running):
            if now < deadline and len(running) < concurrency:
                break
            shard_id, shard_index, cost = pending.pop(0)
            abandoned.append(
                BudgetAbandonedTask(
                    shard_id=shard_id,
                    shard_index=shard_index,
                    logical_cost_ms=cost,
                    reason="round logical-time budget exhausted before task start",
                )
            )
        if not running and not pending:
            break

    return Schedule(
        start_ms=start_ms,
        deadline_ms=deadline,
        started=tuple(started),
        budget_unstarted=tuple(abandoned),
        virtual_end_ms=now,
    )
