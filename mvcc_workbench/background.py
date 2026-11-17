"""Configurable background writer used by the GUI and concurrency tests."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from .engine import Workbench


class BackgroundCommitter:
    def __init__(
        self,
        workbench: Workbench,
        *,
        concurrency: int = 2,
        interval_s: float = 0.2,
        max_commits: int | None = None,
        callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.workbench = workbench
        self.concurrency = max(1, min(32, int(concurrency)))
        self.interval_s = max(0.01, float(interval_s))
        self.max_commits = max_commits
        self.callback = callback
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._futures: list[Future[dict[str, Any]]] = []
        self._submitted = 0
        self._completed = 0
        self._errors = 0
        self._lock = threading.Lock()
        self._started_at_ns: int | None = None
        self._stopped_at_ns: int | None = None
        self._last_result: dict[str, Any] | None = None

    def start(self) -> None:
        if self._scheduler and self._scheduler.is_alive():
            return
        if self.max_commits == 0:
            return
        self.workbench.create_table(
            "background_events",
            [
                {"name": "key", "type": "TEXT", "primary_key": True},
                {"name": "sequence", "type": "INTEGER"},
                {"name": "writer", "type": "INTEGER"},
            ],
        )
        self._stop.clear()
        self._started_at_ns = time.perf_counter_ns()
        self._stopped_at_ns = None
        self._executor = ThreadPoolExecutor(
            max_workers=self.concurrency, thread_name_prefix="mvcc-bg"
        )
        self._scheduler = threading.Thread(target=self._run, name="mvcc-bg-scheduler", daemon=True)
        self._scheduler.start()
        self.workbench.logger.event(
            "background_started", concurrency=self.concurrency, interval_s=self.interval_s
        )

    def _run(self) -> None:
        assert self._executor is not None
        while not self._stop.is_set():
            with self._lock:
                in_flight = len(self._futures) - self._completed
                limit_reached = self.max_commits is not None and self._submitted >= self.max_commits
            if limit_reached:
                break
            if in_flight < self.concurrency:
                with self._lock:
                    sequence = self._submitted + 1
                    self._submitted = sequence
                future = self._executor.submit(self._commit_one, sequence)
                with self._lock:
                    self._futures.append(future)
                future.add_done_callback(self._future_done)
            else:
                self._stop.wait(min(self.interval_s, 0.05))
                continue
            self._stop.wait(self.interval_s)
        self._wait_for_futures()

    def _commit_one(self, sequence: int) -> dict[str, Any]:
        value = {
            "sequence": sequence,
            "writer": threading.get_ident(),
            "monotonic_ns": time.perf_counter_ns(),
        }
        result = self.workbench.auto_commit_write(
            table_name="background_events",
            row_key="counter",
            value=value,
            label=f"background-{sequence}",
        )
        event = {
            "kind": "background_commit",
            "sequence": sequence,
            **result,
        }
        self._last_result = event
        if self.callback:
            self.callback(event)
        return event

    def _future_done(self, future: Future[dict[str, Any]]) -> None:
        with self._lock:
            self._completed += 1
        try:
            future.result()
        except BaseException as exc:
            with self._lock:
                self._errors += 1
            self.workbench.logger.event(
                "background_commit_error", level="error", error=repr(exc)
            )
            if self.callback:
                self.callback({"kind": "background_error", "error": repr(exc)})

    def _wait_for_futures(self) -> None:
        while True:
            with self._lock:
                pending = [future for future in self._futures if not future.done()]
            if not pending:
                return
            self._stop.wait(0.01)

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._scheduler:
            self._scheduler.join(timeout=30)
        if self._executor:
            self._executor.shutdown(wait=True, cancel_futures=False)
        self._stopped_at_ns = time.perf_counter_ns()
        stats = self.stats()
        self.workbench.logger.event("background_stopped", **stats)
        return stats

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "concurrency": self.concurrency,
                "interval_s": self.interval_s,
                "submitted": self._submitted,
                "completed": self._completed,
                "errors": self._errors,
                "running": bool(self._scheduler and self._scheduler.is_alive() and not self._stop.is_set()),
                "started_monotonic_ns": self._started_at_ns,
                "stopped_monotonic_ns": self._stopped_at_ns,
                "last_result": self._last_result,
            }
