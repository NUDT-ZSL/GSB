from __future__ import annotations

import signal
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .errors import MemoryBudgetError


class BoundarySignal:
    """Process signal state; the scheduler observes it only between chunks."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.signum: Optional[int] = None

    def request_stop(self, signum: Optional[int] = None) -> None:
        if self.signum is None:
            self.signum = signum
        self.event.set()

    @property
    def stopped(self) -> bool:
        return self.event.is_set()


def install_signal_handlers(token: BoundarySignal) -> List[str]:
    installed: List[str] = []

    def handler(signum: int, _frame: Any) -> None:
        token.request_stop(signum)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, handler)
            installed.append(name)
        except (ValueError, OSError):
            # Not the main thread or signal unsupported on this platform.
            pass
    return installed


class ChunkCancelled(Exception):
    pass


class OrderedMemoryGate:
    """Bound active sample buffers and grant grants in chunk-index order."""

    def __init__(self, budget_bytes: int):
        if budget_bytes <= 0:
            raise MemoryBudgetError("memory_budget_bytes must be positive")
        self.budget_bytes = int(budget_bytes)
        self.condition = threading.Condition()
        self.used_bytes = 0
        self.active: Dict[int, int] = {}
        self.requested: Dict[int, int] = {}
        self.max_used_bytes = 0
        self.max_active_chunks = 0

    def acquire(
        self,
        ticket: int,
        need_bytes: int,
        cancel_event: Optional[threading.Event] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        if need_bytes > self.budget_bytes:
            raise MemoryBudgetError(
                "one chunk sample buffer exceeds the configured memory budget",
                {
                    "ticket": ticket,
                    "need_bytes": need_bytes,
                    "budget_bytes": self.budget_bytes,
                },
            )
        with self.condition:
            self.requested[ticket] = int(need_bytes)
            while True:
                if (cancel_event is not None and cancel_event.is_set()) or (
                    stop_event is not None and stop_event.is_set()
                ):
                    self.requested.pop(ticket, None)
                    raise ChunkCancelled("chunk cancelled before memory grant")
                lowest_ticket = min(self.requested) if self.requested else None
                if (
                    lowest_ticket == ticket
                    and self.used_bytes + need_bytes <= self.budget_bytes
                ):
                    self.requested.pop(ticket, None)
                    self.active[ticket] = int(need_bytes)
                    self.used_bytes += int(need_bytes)
                    self.max_used_bytes = max(self.max_used_bytes, self.used_bytes)
                    self.max_active_chunks = max(self.max_active_chunks, len(self.active))
                    self.condition.notify_all()
                    return
                self.condition.wait(timeout=0.05)

    def release(self, ticket: int, need_bytes: int) -> None:
        with self.condition:
            self.active.pop(ticket, None)
            self.used_bytes -= int(need_bytes)
            if self.used_bytes < 0:
                self.used_bytes = 0
            self.condition.notify_all()


@dataclass
class SchedulerOutcome:
    successes: List[Any] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    cancelled: List[str] = field(default_factory=list)
    first_error: Optional[BaseException] = None
    interrupted: bool = False
    max_active_chunks: int = 0
    max_used_bytes: int = 0


def run_bounded_chunks(
    chunks: List[Any],
    *,
    max_workers: int,
    memory_budget_bytes: int,
    buffer_size_func: Callable[[Any], int],
    task_func: Callable[[Any], Any],
    signal_token: Optional[BoundarySignal] = None,
) -> SchedulerOutcome:
    ordered_chunks = sorted(chunks, key=lambda chunk: chunk.index)
    worker_count = max(1, min(int(max_workers), len(ordered_chunks) or 1))
    gate = OrderedMemoryGate(memory_budget_bytes)
    cancel_event = threading.Event()
    outcome = SchedulerOutcome()
    outcome.interrupted = signal_token.stopped if signal_token else False

    def worker(chunk: Any) -> Any:
        chunk_id = getattr(chunk, "chunk_id", str(chunk))
        if cancel_event.is_set() or (signal_token and signal_token.stopped):
            raise ChunkCancelled(chunk_id)
        need = int(buffer_size_func(chunk))
        gate.acquire(
            chunk.index,
            need,
            cancel_event=cancel_event,
            stop_event=signal_token.event if signal_token else None,
        )
        try:
            if cancel_event.is_set() or (signal_token and signal_token.stopped):
                raise ChunkCancelled(chunk_id)
            return task_func(chunk)
        finally:
            gate.release(chunk.index, need)

    future_map: Dict[Any, Any] = {}
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="terrain-chunk")
    try:
        for chunk in ordered_chunks:
            future_map[executor.submit(worker, chunk)] = chunk

        for future in as_completed(list(future_map.keys()), timeout=None):
            chunk = future_map[future]
            try:
                result = future.result()
            except ChunkCancelled:
                outcome.cancelled.append(chunk.chunk_id)
            except Exception as exc:  # keep first failure and cancel the rest
                if outcome.first_error is None:
                    outcome.first_error = exc
                    outcome.errors.append(
                        {
                            "chunk_id": chunk.chunk_id,
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                            "traceback": traceback.format_exc(limit=8),
                        }
                    )
                cancel_event.set()
                for pending_future in future_map:
                    pending_future.cancel()
            else:
                outcome.successes.append(result)

            if signal_token and signal_token.stopped:
                cancel_event.set()
                for pending_future in future_map:
                    pending_future.cancel()
                outcome.interrupted = True
                break
            if outcome.first_error is not None:
                break
    finally:
        # cancel_futures is supported by Python 3.9+ and is a no-op for running tasks.
        executor.shutdown(wait=True, cancel_futures=True)

    # Collect any running-task results after orderly shutdown.
    collected = {id(item) for item in outcome.successes}
    for future, chunk in future_map.items():
        if future.done() and not future.cancelled():
            try:
                result = future.result()
            except ChunkCancelled:
                if chunk.chunk_id not in outcome.cancelled:
                    outcome.cancelled.append(chunk.chunk_id)
            except Exception as exc:
                if outcome.first_error is None:
                    outcome.first_error = exc
                    outcome.errors.append(
                        {
                            "chunk_id": chunk.chunk_id,
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                            "traceback": traceback.format_exc(limit=8),
                        }
                    )
            else:
                if id(result) not in collected:
                    outcome.successes.append(result)
                    collected.add(id(result))
        elif future.cancelled():
            if chunk.chunk_id not in outcome.cancelled:
                outcome.cancelled.append(chunk.chunk_id)

    outcome.max_active_chunks = gate.max_active_chunks
    outcome.max_used_bytes = gate.max_used_bytes
    if signal_token:
        outcome.interrupted = outcome.interrupted or signal_token.stopped
    return outcome
