"""Bounded decode/sample budget admission and ordered parallel execution."""

from __future__ import annotations

import threading
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import PipelineError
from .measurement import StopController


@dataclass
class BudgetReservation:
    manager: "BudgetManager"
    decode_bytes: int
    sample_bytes: int
    released: bool = field(default=False)


class BudgetManager:
    """A condition-based two-dimensional admission controller."""

    def __init__(self, decode_budget: int, sample_budget: int) -> None:
        self.decode_budget = decode_budget
        self.sample_budget = sample_budget
        self._condition = threading.Condition()
        self._decode_used = 0
        self._sample_used = 0
        self.peak_decode = 0
        self.peak_samples = 0

    def acquire(self, decode_bytes: int, sample_bytes: int,
                stop_event: threading.Event) -> BudgetReservation:
        if decode_bytes > self.decode_budget:
            raise PipelineError(
                "E_BUDGET",
                f"input needs {decode_bytes} decode bytes > budget {self.decode_budget}",
            )
        if sample_bytes > self.sample_budget:
            raise PipelineError(
                "E_BUDGET",
                f"input needs {sample_bytes} sample bytes > budget {self.sample_budget}",
            )
        with self._condition:
            while (
                self._decode_used + decode_bytes > self.decode_budget
                or self._sample_used + sample_bytes > self.sample_budget
            ):
                if stop_event.is_set():
                    raise PipelineError(
                        "E_BUDGET", "scheduler cancelled while waiting for budget",
                        recoverable=False,
                    )
                self._condition.wait(timeout=0.05)
            self._decode_used += decode_bytes
            self._sample_used += sample_bytes
            self.peak_decode = max(self.peak_decode, self._decode_used)
            self.peak_samples = max(self.peak_samples, self._sample_used)
            return BudgetReservation(self, decode_bytes, sample_bytes)

    def release(self, reservation: BudgetReservation) -> None:
        with self._condition:
            if reservation.released:
                return
            self._decode_used = max(0, self._decode_used - reservation.decode_bytes)
            self._sample_used = max(0, self._sample_used - reservation.sample_bytes)
            reservation.released = True
            self._condition.notify_all()

    def snapshot(self) -> dict[str, int]:
        with self._condition:
            return {
                "decode_budget_bytes": self.decode_budget,
                "sample_budget_bytes": self.sample_budget,
                "decode_in_flight_bytes": self._decode_used,
                "sample_in_flight_bytes": self._sample_used,
                "peak_decode_bytes": self.peak_decode,
                "peak_sample_bytes": self.peak_samples,
            }


@dataclass
class ScheduledItem:
    order: int
    input_id: str
    decode_bytes: int
    sample_bytes: int
    payload: Any


@dataclass
class ScheduledResult:
    order: int
    input_id: str
    value: Any = None
    error: PipelineError | None = None
    completed: bool = True
    sequence: int = 0


def run_bounded(
    items: list[ScheduledItem],
    worker: Callable[[ScheduledItem], Any],
    *,
    max_workers: int,
    budget: BudgetManager,
    stop: StopController,
) -> tuple[list[ScheduledResult], PipelineError | None, dict[str, int]]:
    """Execute items in declaration order with bounded futures and two budgets."""
    if max_workers < 1:
        raise PipelineError("E_CONFIG", "max_workers must be positive")
    results: list[ScheduledResult] = []
    pending: dict[Future, tuple[ScheduledItem, BudgetReservation]] = {}
    first_error: PipelineError | None = None
    fatal_errors: list[tuple[int, int, PipelineError]] = []
    completion_sequence = 0
    sequence_lock = threading.Lock()

    def consume(done: set[Future]) -> None:
        nonlocal first_error, completion_sequence
        for future in done:
            item, reservation = pending.pop(future)
            budget.release(reservation)
            try:
                value = future.result()
                with sequence_lock:
                    completion_sequence += 1
                    sequence = completion_sequence
                results.append(ScheduledResult(
                    order=item.order, input_id=item.input_id, value=value,
                    completed=True, sequence=sequence,
                ))
            except PipelineError as exc:
                if exc.code == "E_INTERRUPTED":
                    continue
                cancellation = (
                    exc.code == "E_INPUT"
                    and isinstance(exc.details.get("first_error"), dict)
                )
                if first_error is None and not cancellation:
                    exc.recoverable = False
                    first_error = exc
                    stop.cancel(exc)
                with sequence_lock:
                    completion_sequence += 1
                    sequence = completion_sequence
                if not cancellation:
                    fatal_errors.append((item.order, sequence, exc))
                results.append(ScheduledResult(
                    order=item.order, input_id=item.input_id, error=exc,
                    completed=False, sequence=sequence,
                ))
            except BaseException as exc:  # pragma: no cover - defensive boundary
                error = PipelineError(
                    "E_RUNTIME", f"unexpected worker failure: {exc}",
                    input_id=item.input_id,
                    details={"traceback": traceback.format_exc()},
                    recoverable=False,
                )
                if first_error is None:
                    first_error = error
                    stop.cancel(error)
                with sequence_lock:
                    completion_sequence += 1
                    sequence = completion_sequence
                results.append(ScheduledResult(
                    order=item.order, input_id=item.input_id, error=error,
                    completed=False, sequence=sequence,
                ))
                fatal_errors.append((item.order, sequence, error))

    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="audio")
    def invoke(item: ScheduledItem, reservation: BudgetReservation) -> Any:
        try:
            return worker(item)
        finally:
            budget.release(reservation)

    try:
        index = 0
        while index < len(items):
            if stop.event.is_set() and (stop.fatal is not None or stop.signal() is not None):
                break
            while len(pending) >= max_workers:
                done, _ = wait(set(pending), return_when=FIRST_COMPLETED)
                consume(done)
                if stop.fatal is not None:
                    break
            if stop.fatal is not None or (stop.event.is_set() and stop.signal() is not None):
                break
            item = items[index]
            try:
                reservation = budget.acquire(
                    item.decode_bytes, item.sample_bytes, stop.event
                )
            except PipelineError as exc:
                exc.input_id = item.input_id
                results.append(ScheduledResult(
                    order=item.order, input_id=item.input_id, error=exc,
                    completed=False, sequence=0,
                ))
                if not exc.recoverable:
                    first_error = first_error or exc
                    stop.cancel(exc)
                    break
                index += 1
                continue
            if stop.event.is_set() and stop.fatal is not None:
                budget.release(reservation)
                break
            future = executor.submit(invoke, item, reservation)
            pending[future] = (item, reservation)
            index += 1
        while pending:
            done, _ = wait(set(pending), return_when=FIRST_COMPLETED)
            consume(done)
    finally:
        stop.event.set()
        for future in pending:
            future.cancel()
        if pending:
            done, _ = wait(set(pending))
            consume(done)
        executor.shutdown(wait=True, cancel_futures=True)
    results.sort(key=lambda item: item.order)
    if fatal_errors:
        first_error = min(fatal_errors, key=lambda entry: (entry[0], entry[1]))[2]
    return results, first_error, budget.snapshot()
