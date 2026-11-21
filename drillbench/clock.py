from __future__ import annotations

import time
from dataclasses import dataclass

from .errors import BusinessError
from .models import ClockSpec


@dataclass
class LogicalClock:
    """A script-driven clock paced by real sleeps.

    The value only changes through explicit advances. Wall-clock time is never
    read or compared; ``time.sleep`` is used solely to pace deterministic
    logical increments on the real transport.
    """

    spec: ClockSpec
    current_ms: int

    @classmethod
    def from_spec(cls, spec: ClockSpec, current_ms: int | None = None) -> "LogicalClock":
        value = spec.start_ms if current_ms is None else max(spec.start_ms, current_ms)
        return cls(spec=spec, current_ms=value)

    @property
    def real_per_logical(self) -> float:
        return self.spec.tick_real_ms / self.spec.tick_logical_ms

    def real_seconds_for_logical(self, logical_ms: int) -> float:
        return max(0.0, logical_ms * self.real_per_logical / 1000.0)

    def advance_to(self, target_ms: int) -> int:
        if target_ms < self.current_ms:
            raise BusinessError(
                "E_CLOCK_MONOTONIC",
                "logical clock cannot move backwards without a rollback injection",
                {"current_ms": self.current_ms, "target_ms": target_ms},
            )
        while self.current_ms < target_ms:
            delta = min(
                self.spec.tick_logical_ms,
                target_ms - self.current_ms,
            )
            time.sleep(self.real_seconds_for_logical(delta))
            self.current_ms += delta
        return self.current_ms

    def advance_by(self, delta_ms: int) -> int:
        if delta_ms < 0:
            raise BusinessError(
                "E_CLOCK_MONOTONIC",
                "logical clock delta cannot be negative",
                {"delta_ms": delta_ms},
            )
        return self.advance_to(self.current_ms + delta_ms)

    def rollback(self, rollback_ms: int) -> tuple[int, int]:
        if rollback_ms <= 0:
            raise BusinessError(
                "E_CLOCK_MONOTONIC",
                "rollback amount must be positive",
                {"rollback_ms": rollback_ms},
            )
        before = self.current_ms
        self.current_ms = max(self.spec.start_ms, self.current_ms - rollback_ms)
        return before, self.current_ms

