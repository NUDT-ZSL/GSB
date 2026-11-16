"""Adaptive backpressure scheduler.

Observables: in-flight limit vs. usage, bounded retry-wait queue length and a
sliding window of recent response latencies.  On rate limiting (429), server
errors (5xx) or rising latency the in-flight limit shrinks multiplicatively
and new work queues; on sustained healthy observations it re-opens by a
bounded additive step.  All parameters are configurable with hard caps, so a
depleted budget never opens unbounded connections.  No fixed sleeps are used
to fake backpressure: waiting happens on a condition variable driven by real
completions, and retry backoff waits are bounded by a semaphore.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager

from ..config import SchedulerConfig


class AdaptiveScheduler:
    def __init__(self, cfg: SchedulerConfig):
        self.cfg = cfg
        self._limit = max(cfg.min_inflight, min(cfg.initial_inflight, cfg.max_inflight))
        self._inflight = 0
        self._cond = asyncio.Condition()
        self._latencies: deque = deque(maxlen=cfg.latency_window)
        self._healthy_streak = 0
        self._retry_sem = asyncio.Semaphore(cfg.retry_queue_max)
        self._retry_waiting = 0
        self.history: list = []  # (event, limit) observations for audit

    # ---- observables ----

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def retry_waiting(self) -> int:
        return self._retry_waiting

    def recent_latency_avg(self) -> float:
        return sum(self._latencies) / len(self._latencies) if self._latencies else 0.0

    # ---- concurrency gate ----

    async def acquire(self) -> None:
        async with self._cond:
            while self._inflight >= self._limit:
                await self._cond.wait()
            self._inflight += 1

    async def release(self) -> None:
        async with self._cond:
            self._inflight -= 1
            self._cond.notify_all()

    @asynccontextmanager
    async def slot(self):
        await self.acquire()
        try:
            yield
        finally:
            await self.release()

    # ---- adaptation ----

    def observe(self, outcome: str, latency_ms: float) -> None:
        """outcome: success | rate_limited | server_error | timeout"""
        cfg = self.cfg
        if latency_ms >= 0:
            self._latencies.append(latency_ms)
        pressured = outcome in ("rate_limited", "server_error", "timeout")
        if not pressured and outcome == "success" and self._latencies:
            pressured = self.recent_latency_avg() > cfg.latency_high_ms
        if pressured:
            new_limit = max(cfg.min_inflight, int(self._limit * cfg.decrease_factor))
            if new_limit != self._limit:
                self.history.append(("shrink", new_limit))
            self._limit = new_limit
            self._healthy_streak = 0
            return
        self._healthy_streak += 1
        # do not re-open while the retry queue is heavily occupied
        queue_busy = self._retry_waiting > cfg.retry_queue_max // 2
        if self._healthy_streak >= cfg.increase_after and not queue_busy:
            new_limit = min(cfg.max_inflight, self._limit + cfg.increase_step)
            if new_limit != self._limit:
                self.history.append(("grow", new_limit))
            self._limit = new_limit
            self._healthy_streak = 0

    # ---- bounded retry backoff ----

    async def retry_wait(self, attempt: int) -> None:
        """Bounded backoff that never occupies an in-flight slot and is capped
        by retry_queue_max concurrent waiters."""
        delay = min(self.cfg.backoff_max_ms,
                    self.cfg.backoff_base_ms * (2 ** max(0, attempt - 1))) / 1000.0
        async with self._retry_sem:
            self._retry_waiting += 1
            try:
                await asyncio.sleep(delay)
            finally:
                self._retry_waiting -= 1

    # ---- checkpointing ----

    def snapshot(self) -> dict:
        return {"limit": self._limit}

    def restore(self, state: dict) -> None:
        limit = int(state.get("limit", self.cfg.initial_inflight))
        self._limit = max(self.cfg.min_inflight, min(limit, self.cfg.max_inflight))
