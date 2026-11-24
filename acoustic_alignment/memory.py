"""Conservative in-flight memory accounting for bounded track scheduling."""
from __future__ import annotations

import threading
from contextlib import contextmanager

from .codes import Code, EngineError


def estimate_track_bytes(frame_count: int, channels: int) -> int:
    """Return a conservative upper bound for decode and FFT/correlation work."""
    frame_count = max(1, int(frame_count))
    channels = max(1, int(channels))
    decoded = frame_count * channels * 8
    mono = frame_count * 8
    correlation = 4 * frame_count * 8
    spectrum = 6 * (frame_count // 2 + 1) * 16
    python_overhead = 2 * 1024 * 1024
    return int(decoded + 4 * mono + correlation + spectrum + python_overhead)


class MemoryAccountant:
    def __init__(self, budget_bytes: int):
        if not isinstance(budget_bytes, int) or isinstance(budget_bytes, bool) or budget_bytes <= 0:
            raise EngineError(Code.E_MEMORY_BUDGET, "memory budget must be a positive integer")
        self.budget_bytes = int(budget_bytes)
        self._condition = threading.Condition()
        self._reserved = 0
        self.peak_reserved = 0

    @property
    def reserved_bytes(self) -> int:
        with self._condition:
            return self._reserved

    def can_fit(self, amount: int) -> bool:
        with self._condition:
            return self._reserved + int(amount) <= self.budget_bytes

    def reserve(self, amount: int) -> None:
        amount = int(amount)
        if amount > self.budget_bytes:
            raise EngineError(
                Code.E_MEMORY,
                "single track working set exceeds memory budget",
                {"required_bytes": amount, "budget_bytes": self.budget_bytes},
            )
        with self._condition:
            while self._reserved + amount > self.budget_bytes:
                self._condition.wait(timeout=0.05)
            self._reserved += amount
            self.peak_reserved = max(self.peak_reserved, self._reserved)

    def release(self, amount: int) -> None:
        amount = int(amount)
        with self._condition:
            self._reserved = max(0, self._reserved - amount)
            self._condition.notify_all()

    @contextmanager
    def lease(self, amount: int):
        self.reserve(amount)
        try:
            yield amount
        finally:
            self.release(amount)
