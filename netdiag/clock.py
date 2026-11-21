from __future__ import annotations


class LogicalClock:
    """A monotonic logical clock quantized to a configured step."""

    def __init__(self, start_ms: int = 0, step_ms: int = 1) -> None:
        if step_ms <= 0:
            raise ValueError("step_ms must be positive")
        self.step_ms = step_ms
        self._now_ms = self._floor_to_step(start_ms)

    def _floor_to_step(self, value: int) -> int:
        return (value // self.step_ms) * self.step_ms

    def _ceil_to_step(self, value: int) -> int:
        return ((value + self.step_ms - 1) // self.step_ms) * self.step_ms

    @property
    def now_ms(self) -> int:
        return self._now_ms

    def advance_to(self, target_ms: int) -> int:
        candidate = self._ceil_to_step(target_ms)
        if candidate > self._now_ms:
            self._now_ms = candidate
        return self._now_ms

    def advance_by(self, delta_ms: int) -> int:
        if delta_ms < 0:
            raise ValueError("logical time cannot move backwards")
        return self.advance_to(self._now_ms + delta_ms)
