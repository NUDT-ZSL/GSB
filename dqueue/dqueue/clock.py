"""Injectable logical clock.

The queue never reads ``time.time``/``datetime.now``.  Every public operation
receives an explicit logical millisecond value which is validated against the
high-water mark persisted in the database.
"""

from .errors import DQError, ErrorCode


class LogicalClock:
    """Monotonic logical clock with a configurable rollback tolerance."""

    def __init__(self, high_water_ms=0, rollback_tolerance_ms=0):
        self._high_water = int(high_water_ms)
        self._tolerance = int(rollback_tolerance_ms)

    @property
    def high_water(self):
        return self._high_water

    def check(self, now_ms):
        if isinstance(now_ms, bool) or not isinstance(now_ms, int):
            raise DQError(
                ErrorCode.E_CLOCK_INVALID,
                "logical clock must be a non-negative integer millisecond value",
                got=repr(now_ms),
            )
        if now_ms < 0:
            raise DQError(
                ErrorCode.E_CLOCK_INVALID,
                "logical clock must be non-negative",
                got=now_ms,
            )
        delta = self._high_water - now_ms
        if delta > self._tolerance:
            raise DQError(
                ErrorCode.E_CLOCK_ROLLBACK,
                "logical clock rolled back beyond tolerance",
                now_ms=now_ms,
                high_water_ms=self._high_water,
                rollback_ms=delta,
                tolerance_ms=self._tolerance,
            )
        return now_ms

    def advance(self, now_ms):
        """Validate and move the high-water mark; returns the new mark."""
        self.check(now_ms)
        if now_ms > self._high_water:
            self._high_water = now_ms
        return self._high_water
