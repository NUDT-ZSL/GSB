from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any, Callable, Iterable


class InterruptController:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.signal_name: str | None = None
        self.exit_code: int | None = None
        self._lock = threading.Lock()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def request(self, signal_name: str, exit_code: int) -> None:
        with self._lock:
            if not self._event.is_set():
                self.signal_name = signal_name
                self.exit_code = exit_code
            self._event.set()

    def reset(self) -> None:
        with self._lock:
            self.signal_name = None
            self.exit_code = None
            self._event.clear()


class MemoryBudget:
    def __init__(self, limit_bytes: int) -> None:
        if limit_bytes <= 0:
            raise ValueError("memory budget must be positive")
        self.limit_bytes = limit_bytes
        self._used = 0
        self._max_seen = 0
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)

    @property
    def used_bytes(self) -> int:
        with self._lock:
            return self._used

    @property
    def max_seen_bytes(self) -> int:
        with self._lock:
            return self._max_seen

    def try_reserve(self, size: int) -> bool:
        if size < 0:
            raise ValueError("reservation size must be non-negative")
        if size > self.limit_bytes:
            return False
        with self._condition:
            if self._used + size > self.limit_bytes:
                return False
            self._used += size
            self._max_seen = max(self._max_seen, self._used)
            return True

    def reserve(self, size: int, *, stop: threading.Event | None = None) -> bool:
        if size > self.limit_bytes:
            return False
        with self._condition:
            while self._used + size > self.limit_bytes:
                if stop is not None and stop.is_set():
                    return False
                self._condition.wait(timeout=0.1)
            self._used += size
            self._max_seen = max(self._max_seen, self._used)
            return True

    def release(self, size: int) -> None:
        if size < 0:
            raise ValueError("release size must be non-negative")
        with self._condition:
            self._used -= size
            if self._used < 0:
                self._used = 0
                raise RuntimeError("memory budget released more than reserved")
            self._condition.notify_all()


class BoundedTaskPool:
    """A thread pool with an explicit, bounded submission window."""

    def __init__(self, max_workers: int) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self.max_workers = max_workers
        self._slots = threading.BoundedSemaphore(max_workers)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="narrative-workbench",
        )
        self._futures: set[Future[Any]] = set()
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        if self._closed:
            raise RuntimeError("task pool is closed")
        self._slots.acquire()

        def invoke() -> Any:
            try:
                return function(*args, **kwargs)
            finally:
                self._slots.release()

        future = self._executor.submit(invoke)
        with self._lock:
            self._futures.add(future)
        future.add_done_callback(self._discard_future)
        return future

    def _discard_future(self, future: Future[Any]) -> None:
        with self._lock:
            self._futures.discard(future)

    def cancel_pending(self) -> None:
        with self._lock:
            futures = list(self._futures)
        for future in futures:
            future.cancel()

    def wait(
        self, futures: Iterable[Future[Any]], *, timeout: float | None = None
    ) -> tuple[set[Future[Any]], set[Future[Any]]]:
        return wait(list(futures), timeout=timeout)

    def shutdown(self, *, wait_for_tasks: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel_pending()
        self._executor.shutdown(wait=wait_for_tasks, cancel_futures=True)

