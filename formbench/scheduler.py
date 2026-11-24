from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import InterruptedBySignal, MemoryBudgetError


class Clock:
    def now(self) -> float:
        raise NotImplementedError

    def advance(self, amount: float) -> None:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> float:
        return time.monotonic()

    def advance(self, amount: float) -> None:
        return None


class ManualClock(Clock):
    def __init__(self, start: float = 0.0) -> None:
        self._value = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._value

    def advance(self, amount: float) -> None:
        with self._lock:
            self._value += float(amount)


@dataclass(frozen=True)
class WorkItem:
    index: int
    buffer_bytes: int
    logic_cost: float
    payload: Any = None


@dataclass
class RoundPlan:
    round_number: int
    indices: list[int]
    projected_bytes: int
    projected_logic: float
    close_reason: str


@dataclass
class SchedulePlan:
    rounds: list[RoundPlan]
    admission_order: list[int]

    @property
    def round_count(self) -> int:
        return len(self.rounds)


def plan_rounds(
    items: list[WorkItem],
    *,
    concurrency: int,
    memory_budget_bytes: int,
    round_logic_budget: float,
) -> SchedulePlan:
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")
    if memory_budget_bytes <= 0:
        raise ValueError("memory_budget_bytes must be positive")
    if round_logic_budget <= 0:
        raise ValueError("round_logic_budget must be positive")

    rounds: list[RoundPlan] = []
    admission_order: list[int] = []
    position = 0
    round_number = 1

    while position < len(items):
        indices: list[int] = []
        used_bytes = 0
        used_logic = 0.0
        close_reason = "all_items_admitted"

        while position < len(items):
            item = items[position]
            if item.buffer_bytes > memory_budget_bytes:
                raise MemoryBudgetError(
                    f"block {item.index} needs {item.buffer_bytes} bytes, "
                    f"exceeding budget {memory_budget_bytes}",
                    field="memory_budget_bytes",
                    details={
                        "block_index": item.index,
                        "required_bytes": item.buffer_bytes,
                        "budget_bytes": memory_budget_bytes,
                    },
                )

            if indices:
                if len(indices) >= concurrency:
                    close_reason = "concurrency_limit"
                    break
                if used_bytes + item.buffer_bytes > memory_budget_bytes:
                    close_reason = "memory_budget"
                    break
                if used_logic + item.logic_cost > round_logic_budget:
                    close_reason = "logic_time_budget"
                    break

            indices.append(item.index)
            used_bytes += item.buffer_bytes
            used_logic += item.logic_cost
            admission_order.append(item.index)
            position += 1

            if used_logic > round_logic_budget:
                close_reason = "block_exceeded_logic_budget"
                break
            if len(indices) >= concurrency:
                close_reason = "concurrency_limit"
                break

        if position >= len(items) and close_reason not in {
            "logic_time_budget",
            "memory_budget",
            "concurrency_limit",
        }:
            close_reason = "all_items_admitted"

        rounds.append(
            RoundPlan(
                round_number=round_number,
                indices=indices,
                projected_bytes=used_bytes,
                projected_logic=round(used_logic, 6),
                close_reason=close_reason,
            )
        )
        round_number += 1

    return SchedulePlan(rounds=rounds, admission_order=admission_order)


class MemoryTracker:
    def __init__(self, budget_bytes: int) -> None:
        self.budget_bytes = int(budget_bytes)
        self.used_bytes = 0
        self.peak_bytes = 0
        self._lock = threading.Lock()

    def reserve(self, amount: int) -> None:
        with self._lock:
            if self.used_bytes + amount > self.budget_bytes:
                raise MemoryBudgetError(
                    f"concurrent pixel buffers would use {self.used_bytes + amount} "
                    f"bytes > budget {self.budget_bytes}"
                )
            self.used_bytes += amount
            self.peak_bytes = max(self.peak_bytes, self.used_bytes)

    def release(self, amount: int) -> None:
        with self._lock:
            self.used_bytes = max(0, self.used_bytes - amount)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "used_bytes": self.used_bytes,
                "peak_bytes": self.peak_bytes,
                "budget_bytes": self.budget_bytes,
            }


class StopController:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.reason: str | None = None
        self.signum: int | None = None

    def request_stop(self, reason: str = "requested", signum: int | None = None) -> None:
        self.reason = reason
        self.signum = signum
        self.event.set()

    @property
    def stopped(self) -> bool:
        return self.event.is_set()


@dataclass
class ExecutionReport:
    completed: list[int] = field(default_factory=list)
    canceled: list[int] = field(default_factory=list)
    rounds: list[dict[str, Any]] = field(default_factory=list)
    peak_buffer_bytes: int = 0


def execute_plan(
    plan: SchedulePlan,
    items_by_index: dict[int, WorkItem],
    *,
    concurrency: int,
    tracker: MemoryTracker,
    clock: Clock,
    stop: StopController | None = None,
    worker: Callable[[WorkItem], Any],
    on_result: Callable[[WorkItem, Any], None] | None = None,
) -> ExecutionReport:
    """Execute planned rounds. Exceptions cancel pending work and release buffers."""

    stop = stop or StopController()
    report = ExecutionReport()
    completed_set: set[int] = set()
    all_indices = [item.index for item in items_by_index.values()]

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        for round_plan in plan.rounds:
            if stop.stopped:
                report.canceled.extend(
                    index for index in all_indices if index not in completed_set
                )
                raise InterruptedBySignal(
                    "stop requested at chunk boundary",
                    reason_code="E_INTERRUPTED_BOUNDARY",
                    details={"completed": sorted(completed_set)},
                )

            started = clock.now()
            futures: dict[Future[Any], tuple[WorkItem, bool]] = {}

            def run_item(item: WorkItem) -> Any:
                try:
                    clock.advance(item.logic_cost)
                    return worker(item)
                finally:
                    tracker.release(item.buffer_bytes)

            for index in round_plan.indices:
                item = items_by_index[index]
                tracker.reserve(item.buffer_bytes)
                try:
                    future = executor.submit(run_item, item)
                except BaseException:
                    tracker.release(item.buffer_bytes)
                    for pending_future in futures:
                        pending_future.cancel()
                    raise
                futures[future] = (item, True)

            first_error: BaseException | None = None
            for future in as_completed(list(futures.keys())):
                item, _ = futures[future]
                try:
                    value = future.result()
                except BaseException as exc:  # cancellation/processing errors propagate later
                    if first_error is None:
                        first_error = exc
                    continue
                if item.index in completed_set:
                    continue
                completed_set.add(item.index)
                report.completed.append(item.index)
                if on_result is not None:
                    try:
                        on_result(item, value)
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc

            if first_error is not None:
                for future in futures:
                    if future.cancel():
                        item, _ = futures[future]
                        tracker.release(item.buffer_bytes)
                remaining = [
                    index
                    for index in all_indices
                    if index not in completed_set
                ]
                report.canceled.extend(remaining)
                executor.shutdown(wait=True, cancel_futures=True)
                raise first_error

            elapsed = clock.now() - started
            snapshot = tracker.snapshot()
            report.peak_buffer_bytes = snapshot["peak_bytes"]
            report.rounds.append(
                {
                    "round_number": round_plan.round_number,
                    "admitted": list(round_plan.indices),
                    "projected_bytes": round_plan.projected_bytes,
                    "projected_logic": round_plan.projected_logic,
                    "elapsed_logic": round(elapsed, 6),
                    "close_reason": round_plan.close_reason,
                    "buffer_after_round": snapshot["used_bytes"],
                    "peak_buffer_bytes": snapshot["peak_bytes"],
                }
            )

            if stop.stopped:
                remaining = [index for index in all_indices if index not in completed_set]
                report.canceled.extend(remaining)
                raise InterruptedBySignal(
                    "stop requested after chunk boundary",
                    reason_code="E_INTERRUPTED_BOUNDARY",
                    details={"completed": sorted(completed_set)},
                )

    report.completed.sort()
    report.canceled = sorted(set(report.canceled) - set(report.completed))
    report.peak_buffer_bytes = tracker.peak_bytes
    return report
