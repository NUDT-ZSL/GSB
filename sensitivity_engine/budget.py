"""Real wall-clock budgets and bounded in-memory batch queue."""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from .codes import Code
from .models import issue

T = TypeVar("T")
R = TypeVar("R")


class BudgetTimeout(RuntimeError):
    def __init__(self, elapsed_ms: float, limit_ms: float) -> None:
        super().__init__(f"batch exceeded wall-clock budget: {elapsed_ms:.3f}ms > {limit_ms}ms")
        self.elapsed_ms = elapsed_ms
        self.limit_ms = limit_ms


@dataclass
class WallBudget:
    limit_ms: float | None
    start_perf: float

    @classmethod
    def start(cls, limit_ms: float | None) -> "WallBudget":
        return cls(limit_ms=limit_ms, start_perf=time.perf_counter())

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.start_perf) * 1000.0

    def check(self) -> None:
        if self.limit_ms is None:
            return
        elapsed = self.elapsed_ms()
        # A zero budget still permits the first boundary to record partial work
        # before rollback; callers invoke check() after each write.
        if elapsed >= self.limit_ms:
            raise BudgetTimeout(elapsed, self.limit_ms)


class BoundedBatchQueue(Generic[T]):
    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("queue capacity must be at least 1")
        self.capacity = capacity
        self._queue: queue.Queue[T] = queue.Queue(maxsize=capacity)

    def submit(self, item: T) -> bool:
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            return False

    def put_blocking(self, item: T) -> None:
        self._queue.put(item)

    def get(self, timeout: float | None = None) -> T:
        return self._queue.get(timeout=timeout)

    def task_done(self) -> None:
        self._queue.task_done()

    def join(self) -> None:
        self._queue.join()


def run_pool(
    items: list[T],
    worker: Callable[[T], R],
    *,
    concurrency: int,
    queue_limit: int,
) -> tuple[list[tuple[int, R | None]], list[dict[str, Any]]]:
    """Run deterministic indexing work through a bounded queue and threads."""
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if queue_limit < 1:
        raise ValueError("queue_limit must be at least 1")

    task_queue: BoundedBatchQueue[tuple[int, T] | None] = BoundedBatchQueue(queue_limit)
    results: list[tuple[int, R | None]] = [(index, None) for index in range(len(items))]
    queue_rejections: list[dict[str, Any]] = []
    errors: dict[int, BaseException] = {}

    def loop() -> None:
        while True:
            try:
                wrapped = task_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                if wrapped is None:
                    return
                index, item = wrapped
                try:
                    results[index] = (index, worker(item))
                except BaseException as exc:  # captured for deterministic report
                    errors[index] = exc
            finally:
                task_queue.task_done()

    import threading

    threads = [threading.Thread(target=loop, name=f"batch-worker-{i}", daemon=True) for i in range(concurrency)]
    for thread in threads:
        thread.start()

    accepted = 0
    for index, item in enumerate(items):
        if task_queue.submit((index, item)):
            accepted += 1
        else:
            if isinstance(item, dict):
                record_key = str(item.get("batch_id", index))
            else:
                record_key = str(getattr(item, "batch_id", index))
            queue_rejections.append(
                issue(
                    Code.QUEUE_FULL,
                    "pending batch queue is full; batch was rejected",
                    record_key=record_key,
                    queue_limit=queue_limit,
                ).to_dict()
            )

    task_queue.join()
    for _ in threads:
        task_queue.put_blocking(None)  # type: ignore[arg-type]
    for thread in threads:
        thread.join(timeout=2)

    for index, exc in sorted(errors.items()):
        results[index] = (index, None)
        queue_rejections.append(
            issue(
                Code.INVALID_RECORD,
                "worker failed while processing input",
                record_key=str(index),
                details=str(exc),
            ).to_dict()
        )

    return results, queue_rejections
