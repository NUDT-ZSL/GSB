"""Injected logical clock.

All scheduling time decisions read this clock only - never :func:`time.time`.
Each :class:`~datapipe.model.ClockStep` both advances the logical clock and
performs a bounded real sleep, so a timeout requires genuine clock advancement
plus real waiting instead of being labelled after the fact.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Tuple

from .errors import E_CLOCK_ROLLBACK, E_CLOCK_STEPS_EXHAUSTED, fail
from .model import ClockStep


@dataclass
class LogicalClock:
    steps: Tuple[ClockStep, ...]
    rollback_tolerance_ms: int = 0
    cursor: int = 0
    now_ms: int = 0
    high_water_ms: int = 0

    def advance(self, reason: str) -> int:
        """Consume one injected step, sleeping for its real wait component."""
        if self.cursor >= len(self.steps):
            raise fail(
                E_CLOCK_STEPS_EXHAUSTED,
                "logical clock step table exhausted",
                reason=reason,
                consumed=self.cursor,
            )
        step = self.steps[self.cursor]
        self.cursor += 1
        candidate = self.now_ms + step.advance_ms
        # An injected step can explicitly move the clock backwards to test
        # rollback protection; moves beyond tolerance are rejected.
        if candidate < self.high_water_ms:
            delta = self.high_water_ms - candidate
            if delta > self.rollback_tolerance_ms:
                raise fail(
                    E_CLOCK_ROLLBACK,
                    "logical clock rolled back beyond tolerance",
                    reason=reason,
                    high_water_ms=self.high_water_ms,
                    candidate_ms=candidate,
                    tolerance_ms=self.rollback_tolerance_ms,
                )
        if step.sleep_ms > 0:
            time.sleep(step.sleep_ms / 1000.0)
        self.now_ms = candidate
        self.high_water_ms = max(self.high_water_ms, candidate)
        return self.now_ms

    def peek_next(self) -> ClockStep:
        if self.cursor >= len(self.steps):
            raise fail(E_CLOCK_STEPS_EXHAUSTED, "no further clock steps")
        return self.steps[self.cursor]

    def snapshot(self) -> dict:
        return {
            "now_ms": self.now_ms,
            "high_water_ms": self.high_water_ms,
            "consumed_steps": self.cursor,
            "total_steps": len(self.steps),
        }
