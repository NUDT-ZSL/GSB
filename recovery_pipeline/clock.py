"""Injectable logical clock.

All effective-time computations, action windows and rollback instants are
derived exclusively from this clock; the pipeline never reads the wall clock.
The clock is fed an explicit sequence of millisecond values, which makes
replays bit-for-bit deterministic.
"""

from __future__ import annotations

from collections.abc import Iterable

from .errors import BizError, Err


class LogicalClock:
    """A scripted logical clock with rollback-tolerance enforcement."""

    def __init__(self, values, rollback_tolerance_ms: int = 0) -> None:
        self._values = [int(v) for v in values]
        self._idx = 0
        self._last = None
        self.rollback_tolerance_ms = int(rollback_tolerance_ms)

    def now_ms(self) -> int:
        """Return the next logical-clock reading in milliseconds."""
        if self._idx >= len(self._values):
            raise BizError(
                Err.CLOCK_ROLLBACK_EXCEEDED,
                "logical clock sequence exhausted",
                readings=self._idx,
            )
        value = self._values[self._idx]
        self._idx += 1
        if self._last is not None and value < self._last - self.rollback_tolerance_ms:
            raise BizError(
                Err.CLOCK_ROLLBACK_EXCEEDED,
                "logical clock rolled back beyond tolerance",
                previous_ms=self._last,
                current_ms=value,
                rollback_ms=self._last - value,
                tolerance_ms=self.rollback_tolerance_ms,
            )
        self._last = value
        return value

    @property
    def last_ms(self):
        return self._last

    @property
    def readings(self) -> int:
        return self._idx
