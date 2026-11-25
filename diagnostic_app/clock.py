from __future__ import annotations

import threading


class LogicalClock:
    """Monotonic logical millisecond clock.

    Samples use an absolute but fixed logical epoch.  This keeps v2 metadata and
    snapshot ledgers deterministic without reading the wall clock.
    """

    def __init__(self, start_ms: int = 1_735_689_600_000) -> None:
        self._value = int(start_ms)
        self._lock = threading.Lock()

    def now_ms(self) -> int:
        with self._lock:
            return self._value

    def advance(self, milliseconds: int = 1) -> int:
        if milliseconds < 0:
            raise ValueError("logical clock cannot move backwards")
        with self._lock:
            self._value += int(milliseconds)
            return self._value

    def next_ms(self) -> int:
        return self.advance(1)
