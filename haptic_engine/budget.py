from __future__ import annotations

import heapq
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


class BudgetCancelled(Exception):
    pass


@dataclass
class _Waiter:
    device_id: str
    block_index: int
    ticket: int
    count: int


class InflightBudget:
    def __init__(self, limit: int) -> None:
        if limit <= 0:
            raise ValueError("inflight limit must be positive")
        self.limit = limit
        self._available = limit
        self._next_ticket = 0
        self._waiters: list[tuple[str, int, int]] = []
        self._waiters_by_ticket: dict[int, _Waiter] = {}
        self._condition = threading.Condition()
        self.peak = 0
        self.wait_events = 0
        self.total_wait_seconds = 0.0

    def acquire(
        self,
        device_id: str,
        block_index: int,
        count: int,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if count < 0:
            raise ValueError("count must be non-negative")
        if count == 0:
            return
        if count > self.limit:
            raise ValueError("request exceeds global inflight limit")
        with self._condition:
            self._next_ticket += 1
            waiter = _Waiter(
                device_id=device_id,
                block_index=block_index,
                ticket=self._next_ticket,
                count=count,
            )
            heapq.heappush(
                self._waiters,
                (waiter.device_id, waiter.block_index, waiter.ticket),
            )
            self._waiters_by_ticket[waiter.ticket] = waiter
            started_waiting = False
            wait_started_at = 0.0
            try:
                while True:
                    if cancelled is not None and cancelled():
                        raise BudgetCancelled()
                    head_key = self._waiters[0]
                    head_waiter = self._waiters_by_ticket[head_key[2]]
                    if (
                        head_waiter.ticket == waiter.ticket
                        and waiter.count <= self._available
                    ):
                        heapq.heappop(self._waiters)
                        self._waiters_by_ticket.pop(waiter.ticket, None)
                        self._available -= waiter.count
                        in_flight = self.limit - self._available
                        self.peak = max(self.peak, in_flight)
                        if started_waiting:
                            self.wait_events += 1
                            self.total_wait_seconds += time.monotonic() - wait_started_at
                        return
                    if not started_waiting:
                        started_waiting = True
                        wait_started_at = time.monotonic()
                    self._condition.wait(timeout=0.05)
            finally:
                if waiter.ticket in self._waiters_by_ticket:
                    self._waiters_by_ticket.pop(waiter.ticket, None)
                    for index, key in enumerate(self._waiters):
                        if key[2] == waiter.ticket:
                            del self._waiters[index]
                            heapq.heapify(self._waiters)
                            self._condition.notify_all()
                            break

    def release(self, count: int) -> None:
        if count <= 0:
            return
        with self._condition:
            self._available += count
            if self._available > self.limit:
                raise AssertionError("inflight budget released too many permits")
            self._condition.notify_all()

    def snapshot(self) -> dict[str, int | float]:
        with self._condition:
            return {
                "limit": self.limit,
                "in_flight": self.limit - self._available,
                "peak": self.peak,
                "wait_events": self.wait_events,
                "wait_seconds": round(self.total_wait_seconds, 9),
            }
