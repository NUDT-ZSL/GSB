from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import AnimRecalcError, InterruptedAtChunk


ChunkTask = Callable[[int, threading.Event], dict[str, Any]]


@dataclass
class SchedulerOutcome:
    results: dict[int, dict[str, Any]] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    submitted: list[int] = field(default_factory=list)
    completed_order: list[int] = field(default_factory=list)
    cancelled: list[int] = field(default_factory=list)
    memory_wait_events: int = 0
    stop_requested: bool = False

    @property
    def first_error(self) -> dict[str, Any] | None:
        return self.errors[0] if self.errors else None


class BoundedChunkScheduler:
    """Schedule chunks in index order with worker and in-flight byte bounds."""

    def __init__(
        self,
        max_workers: int,
        memory_budget_bytes: int,
        estimate_bytes: Callable[[int], int],
        stop_event: threading.Event | None = None,
    ):
        self.max_workers = max(1, int(max_workers))
        self.memory_budget_bytes = max(1, int(memory_budget_bytes))
        self.estimate_bytes = estimate_bytes
        self.stop_event = stop_event or threading.Event()

    def run(
        self,
        indices: list[int],
        task: ChunkTask,
        on_complete: Callable[[int, dict[str, Any]], None] | None = None,
    ) -> SchedulerOutcome:
        outcome = SchedulerOutcome()
        ordered = sorted(indices)
        next_position = 0
        in_flight_bytes = 0
        future_info: dict[Future[dict[str, Any]], tuple[int, int]] = {}

        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="anim-chunk") as pool:
            while next_position < len(ordered) or future_info:
                if self.stop_event.is_set():
                    outcome.stop_requested = True
                    while next_position < len(ordered):
                        outcome.cancelled.append(ordered[next_position])
                        next_position += 1

                while next_position < len(ordered) and not self.stop_event.is_set():
                    index = ordered[next_position]
                    estimate = max(1, int(self.estimate_bytes(index)))
                    if estimate > self.memory_budget_bytes and not future_info:
                        raise AnimRecalcError(
                            "E_MEMORY_BUDGET",
                            "a single chunk exceeds the configured memory budget",
                            {
                                "chunk_index": index,
                                "estimated_bytes": estimate,
                                "budget_bytes": self.memory_budget_bytes,
                            },
                        )
                    if len(future_info) >= self.max_workers:
                        break
                    if future_info and in_flight_bytes + estimate > self.memory_budget_bytes:
                        outcome.memory_wait_events += 1
                        break
                    if not future_info and estimate > self.memory_budget_bytes:
                        break
                    future = pool.submit(task, index, self.stop_event)
                    future_info[future] = (index, estimate)
                    outcome.submitted.append(index)
                    in_flight_bytes += estimate
                    next_position += 1

                if not future_info:
                    break

                done, _ = wait(future_info.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    index, estimate = future_info.pop(future)
                    in_flight_bytes -= estimate
                    try:
                        result = future.result()
                    except InterruptedAtChunk:
                        self.stop_event.set()
                        outcome.stop_requested = True
                        continue
                    except Exception as exc:  # reported as first task error
                        error = {
                            "chunk_index": index,
                            "type": type(exc).__name__,
                            "code": getattr(exc, "code", "E_CHUNK_FAILED"),
                            "message": str(exc),
                            "details": getattr(exc, "details", {}),
                        }
                        if not outcome.errors:
                            outcome.errors.append(error)
                        else:
                            outcome.errors.append(error)
                        continue
                    try:
                        outcome.results[index] = result
                        outcome.completed_order.append(index)
                        if on_complete is not None:
                            on_complete(index, result)
                    except Exception as exc:
                        error = {
                            "chunk_index": index,
                            "type": type(exc).__name__,
                            "code": getattr(exc, "code", "E_PERSIST_FAILED"),
                            "message": str(exc),
                            "details": getattr(exc, "details", {}),
                        }
                        outcome.errors.append(error)
                        self.stop_event.set()

                if outcome.errors:
                    self.stop_event.set()
                    for future in list(future_info.keys()):
                        index, _ = future_info[future]
                        if future.cancel():
                            outcome.cancelled.append(index)
                    # Running futures are allowed to finish so their atomic chunks
                    # are either fully present or absent; queued futures are cancelled.

            if outcome.errors:
                for future in list(future_info.keys()):
                    index, _ = future_info[future]
                    try:
                        result = future.result()
                    except InterruptedAtChunk:
                        self.stop_event.set()
                        outcome.stop_requested = True
                        continue
                    except Exception as exc:
                        error = {
                            "chunk_index": index,
                            "type": type(exc).__name__,
                            "code": getattr(exc, "code", "E_CHUNK_FAILED"),
                            "message": str(exc),
                            "details": getattr(exc, "details", {}),
                        }
                        outcome.errors.append(error)
                    else:
                        outcome.results[index] = result
                        outcome.completed_order.append(index)
                        if on_complete is not None:
                            on_complete(index, result)

        return outcome
