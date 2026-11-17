"""Per-group aggregate state and per-partition window state containers."""

from __future__ import annotations

from dataclasses import dataclass, field

from .fixedpoint import FIXED_POINT_SCALE, mean_scaled
from .ring import RingBuffer, RingEntry


@dataclass
class GroupState:
    """Fixed-point integer aggregate state for one group key."""

    count: int = 0
    total: int = 0
    minimum: int | None = None
    maximum: int | None = None
    numeric_updates: int = 0
    seen_row_ids: set[int] = field(default_factory=set)

    def update(self, row_id: int, value: int | None) -> None:
        if row_id in self.seen_row_ids:
            return
        self.seen_row_ids.add(row_id)
        self.count += 1
        if value is not None:
            self.numeric_updates += 1
            self.total += value
            self.minimum = value if self.minimum is None else min(self.minimum, value)
            self.maximum = value if self.maximum is None else max(self.maximum, value)

    def track_sum(self, row_id: int, value: int) -> None:
        if row_id in self.seen_row_ids:
            return
        self.seen_row_ids.add(row_id)
        self.count += 1
        self.total += value

    def track_extreme(self, row_id: int, value: int, is_min: bool) -> None:
        if row_id in self.seen_row_ids:
            return
        self.seen_row_ids.add(row_id)
        self.count += 1
        if is_min:
            self.minimum = value if self.minimum is None else min(self.minimum, value)
        else:
            self.maximum = value if self.maximum is None else max(self.maximum, value)

    def track_mean(self, row_id: int, value: int) -> None:
        if row_id in self.seen_row_ids:
            return
        self.seen_row_ids.add(row_id)
        self.count += 1
        self.numeric_updates += 1
        self.total += value

    def mean_scaled(self, scale: int = FIXED_POINT_SCALE) -> int | None:
        if self.numeric_updates == 0:
            return None
        return mean_scaled(self.total, self.numeric_updates, scale)

    def summary(self, scale: int = FIXED_POINT_SCALE) -> dict:
        return {
            "count": self.count,
            "sum": self.total,
            "min": self.minimum,
            "max": self.maximum,
            "mean_scaled": self.mean_scaled(scale),
        }

    def to_state(self) -> dict:
        return {
            "count": self.count,
            "sum": self.total,
            "min": self.minimum,
            "max": self.maximum,
            "numeric_updates": self.numeric_updates,
            "seen_row_ids": sorted(self.seen_row_ids),
        }

    @classmethod
    def from_state(cls, state: dict) -> "GroupState":
        return cls(
            count=int(state["count"]),
            total=int(state["sum"]),
            minimum=state["min"],
            maximum=state["max"],
            numeric_updates=int(state.get("numeric_updates", 0)),
            seen_row_ids=set(state["seen_row_ids"]),
        )


@dataclass
class WindowPartitionState:
    """Ring buffer plus per-row emissions for one window partition."""

    ring: RingBuffer
    watermark: int | None = None

    def to_state(self) -> dict:
        return {
            "watermark": self.watermark,
            "ring": self.ring.to_state(),
        }

    @classmethod
    def from_state(cls, state: dict, max_rows: int) -> "WindowPartitionState":
        ring = RingBuffer.from_state(state["ring"]) if state.get("ring") else RingBuffer(max_rows)
        return cls(ring=ring, watermark=state["watermark"])
