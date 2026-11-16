"""Clock basis definition.

Clock basis (mandatory, written into every sample/snapshot):
  * Reference   : time.monotonic_ns() of the load-driver process.
  * Unit        : integer milliseconds, truncated from nanoseconds.
  * Guarantee   : monotonic within one process; wall time is NEVER used for
                  latency or duration measurement.
  * Injection   : `rollback_ms` shifts the reading backwards once at a given
                  tick index to emulate a backwards-moving monotonic source.
                  Injection changes only an input parameter, never this source.

Truth source for request success:
  * The local mock service's JSON receipt (see capbench.mock_server). A request
    counts as success only when a parseable receipt with status="ok" is returned.
    Transport errors, parse errors and status="error" receipts are failures.
    The server appends every receipt to its own log file, which is the ground
    truth available for independent post-run auditing.
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import ClockError

CLOCK_BASIS = "monotonic_ns->integer_ms"


class MonotonicClock:
    """Monotonic millisecond clock with optional deterministic rollback injection."""

    def __init__(self, rollback_ms: int = 0, rollback_at_tick: int = 8) -> None:
        self._origin_ns = _monotonic_ns()
        self._rollback_ms = int(rollback_ms)
        self._rollback_at_tick = int(rollback_at_tick)
        self._tick = 0
        self._last_ms = 0
        self.rollback_fired = False

    def now_ms(self) -> int:
        raw = (_monotonic_ns() - self._origin_ns) // 1_000_000
        self._tick += 1
        if (not self.rollback_fired and self._rollback_ms > 0
                and self._tick >= self._rollback_at_tick):
            raw -= self._rollback_ms
            self.rollback_fired = True
        if raw < self._last_ms:
            raise ClockError(
                "sampling clock moved backwards",
                {
                    "previous_ms": self._last_ms,
                    "observed_ms": raw,
                    "delta_ms": raw - self._last_ms,
                    "clock_basis": CLOCK_BASIS,
                },
            )
        self._last_ms = raw
        return raw


def _monotonic_ns() -> int:
    import time
    return time.monotonic_ns()


@dataclass(frozen=True)
class ClockSpec:
    basis: str = CLOCK_BASIS
    unit: str = "integer_millisecond"
    truth_source: str = "mock_server_receipt_and_server_log"
