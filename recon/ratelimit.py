"""令牌桶/滑动窗口限速、并发闸门与最小间隔控制。

- SlidingWindowRateLimiter：任意连续 1000ms 窗口内请求数 <= QPS。
- ConcurrencyGate：在途请求数 <= 并发上限。
- IntervalLimiter：相邻两次请求发起间隔 >= 配置毫秒数（时间戳源）。

所有等待均按小步长 sleep，并检查 shutdown 事件——中断信号可立即打断等待。
统计被动等待毫秒数与当前在途请求数。
"""
from __future__ import annotations

import threading
import time
from collections import deque

from .errors import StoppedError

_TICK_MS = 20.0


class _WaitStats:
    def __init__(self) -> None:
        self.wait_ms = 0.0
        self.completed_requests = 0
        self.in_flight = 0
        self._lock = threading.Lock()

    def add_wait(self, ms: float) -> None:
        with self._lock:
            self.wait_ms += ms

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "completed_requests": self.completed_requests,
                "wait_ms": round(self.wait_ms, 2),
                "in_flight": self.in_flight,
            }


def _interruptible_sleep(seconds: float, shutdown: threading.Event,
                         stats: _WaitStats | None = None) -> None:
    """可被 shutdown 事件打断的睡眠；被打断抛 StoppedError。"""
    deadline = time.monotonic() + seconds
    while True:
        if shutdown.is_set():
            raise StoppedError("等待被中断信号打断")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        step = min(_TICK_MS / 1000.0, remaining)
        t0 = time.monotonic()
        if shutdown.wait(step):
            if stats is not None:
                stats.add_wait((time.monotonic() - t0) * 1000.0)
            raise StoppedError("等待被中断信号打断")
        if stats is not None:
            stats.add_wait((time.monotonic() - t0) * 1000.0)


class SlidingWindowRateLimiter:
    """滑动 1 秒窗口限流器（满足"任意连续 1000ms 窗口不超过 QPS"）。"""

    def __init__(self, qps: float, shutdown: threading.Event, stats: _WaitStats,
                 window_ms: float = 1000.0):
        self.qps = qps
        self.window_ms = window_ms
        self._stamps: deque[float] = deque()
        self._cond = threading.Lock()
        self._shutdown = shutdown
        self._stats = stats

    def acquire(self) -> None:
        while True:
            wait_s = 0.0
            with self._cond:
                now = time.monotonic()
                cutoff = now - self.window_ms / 1000.0
                while self._stamps and self._stamps[0] <= cutoff:
                    self._stamps.popleft()
                if len(self._stamps) < self.qps:
                    self._stamps.append(now)
                    return
                # 最早一个时间戳滑出窗口后即可放行
                wait_s = (self._stamps[0] + self.window_ms / 1000.0) - now
            _interruptible_sleep(max(wait_s, 0.005), self._shutdown, self._stats)


class ConcurrencyGate:
    def __init__(self, limit: int, shutdown: threading.Event, stats: _WaitStats):
        self._sem = threading.BoundedSemaphore(limit)
        self._shutdown = shutdown
        self._stats = stats

    def acquire(self) -> None:
        while True:
            if self._shutdown.is_set():
                raise StoppedError("并发等待被中断")
            if self._sem.acquire(timeout=_TICK_MS / 1000.0):
                self._stats.in_flight += 1
                return

    def release(self) -> None:
        self._stats.in_flight -= 1
        self._sem.release()


class IntervalLimiter:
    """相邻请求发起时间间隔 >= min_interval_ms（单调调度，不累积漂移）。"""

    def __init__(self, min_interval_ms: float, shutdown: threading.Event, stats: _WaitStats):
        self.min_interval_s = min_interval_ms / 1000.0
        self._next_allowed: float | None = None
        self._lock = threading.Lock()
        self._shutdown = shutdown
        self._stats = stats

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._next_allowed is None:
                # 首次请求立即放行，下一允许时刻 = 现在 + 间隔
                self._next_allowed = now + self.min_interval_s
                return
            wait_s = self._next_allowed - now
            if wait_s <= 0:
                # 已超出计划时刻（例如被其他工作拖慢）：立即放行并顺延
                self._next_allowed = max(self._next_allowed, now) + self.min_interval_s
                return
        # 锁外等待，可被中断
        _interruptible_sleep(wait_s, self._shutdown, self._stats)
        with self._lock:
            # 严格按计划时刻推进，保证相邻发起间隔 >= min_interval
            self._next_allowed += self.min_interval_s
