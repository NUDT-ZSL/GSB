from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class ClockSegment:
    from_ms: int
    to_ms: int
    step_ms: int


@dataclass(frozen=True)
class ClockSpec:
    """A pure logical clock step table.

    Segments are contiguous and half-open: ``[from_ms, to_ms)``.  A clock
    value is only ever derived from these values; no wall-clock API is used.
    """

    segments: tuple[ClockSegment, ...]
    start_ms: int = 0

    def segment_at(self, logical_ms: int) -> ClockSegment:
        for segment in self.segments:
            if segment.from_ms <= logical_ms < segment.to_ms:
                return segment
        if self.segments and logical_ms == self.segments[-1].to_ms:
            return self.segments[-1]
        raise ValueError(f"logical time {logical_ms} is outside the clock table")

    def ticks_between(self, start_ms: int, end_ms: int) -> Iterator[int]:
        """Yield logical ticks in ``[start_ms, end_ms)``.

        Tick alignment restarts at segment boundaries.  This makes the table
        itself the source of truth and avoids accumulation drift.
        """

        if end_ms <= start_ms:
            return
        current = start_ms
        while current < end_ms:
            yield current
            segment = self.segment_at(current)
            candidate = current + segment.step_ms
            if candidate >= segment.to_ms:
                candidate = segment.to_ms
            if candidate <= current:
                raise ValueError("logical clock did not advance")
            current = candidate


class InjectedLogicalClock:
    """Mutable cursor over a :class:`ClockSpec`.

    The cursor is intentionally independent of the host clock.  Callers move it
    with explicit logical millisecond values.
    """

    def __init__(self, spec: ClockSpec, start_ms: int = 0) -> None:
        self.spec = spec
        self._now_ms = start_ms

    @property
    def now_ms(self) -> int:
        return self._now_ms

    def advance_to(self, logical_ms: int) -> int:
        if logical_ms < self._now_ms:
            raise ValueError("logical clock cannot move backwards")
        self._now_ms = logical_ms
        return self._now_ms

    def ticks_until(self, end_ms: int) -> Iterator[int]:
        for tick in self.spec.ticks_between(self._now_ms, end_ms):
            self._now_ms = tick
            yield tick

