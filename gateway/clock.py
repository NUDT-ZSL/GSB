"""Injectable logical clock. The engine never reads the wall clock; all
window boundaries, queue waits and retry backoffs derive from this clock.
"""
from . import errors
from .errors import GatewayError


class LogicalClock:
    def __init__(self, rollback_threshold_ms):
        self.rollback_threshold_ms = rollback_threshold_ms
        self.now_ms = 0

    def advance_to(self, ts_ms):
        """Advance the clock to ts_ms. A rollback beyond the configured
        threshold raises E_CLOCK_ROLLBACK; small rollbacks are tolerated
        and clamped (the clock is monotonic)."""
        if ts_ms < self.now_ms:
            delta = self.now_ms - ts_ms
            if delta > self.rollback_threshold_ms:
                raise GatewayError(
                    errors.E_CLOCK_ROLLBACK,
                    "logical clock rolled back beyond threshold",
                    now_ms=self.now_ms, requested_ms=ts_ms,
                    rollback_ms=delta,
                    threshold_ms=self.rollback_threshold_ms)
            return self.now_ms
        self.now_ms = ts_ms
        return self.now_ms
