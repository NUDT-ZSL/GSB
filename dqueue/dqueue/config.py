"""Static configuration objects for schedulers and adaptive switching."""

from dataclasses import dataclass, field, asdict


@dataclass(frozen=True)
class WheelConfig:
    """Hierarchical timing wheel geometry.

    Level 0 has ``ticks_per_wheel`` slots of ``tick_ms`` milliseconds; every
    higher level spans ``ticks_per_wheel`` slots of the previous level's full
    range.  Total horizon is ``tick_ms * ticks_per_wheel ** level_count``.
    """

    tick_ms: int = 100
    ticks_per_wheel: int = 8
    level_count: int = 3

    def validate(self):
        if not (isinstance(self.tick_ms, int) and self.tick_ms > 0):
            raise ValueError("wheel.tick_ms must be a positive integer")
        if not (isinstance(self.ticks_per_wheel, int) and self.ticks_per_wheel >= 2):
            raise ValueError("wheel.ticks_per_wheel must be >= 2")
        if not (isinstance(self.level_count, int) and self.level_count >= 1):
            raise ValueError("wheel.level_count must be >= 1")
        return self

    def level_width(self, level):
        return self.tick_ms * self.ticks_per_wheel ** level

    @property
    def horizon_ms(self):
        return self.tick_ms * self.ticks_per_wheel ** self.level_count


@dataclass(frozen=True)
class AdaptiveConfig:
    """Rules for the heap <-> wheel mode switch.

    Estimated scan cost per logical advance (see ``scheduler.estimate_cost``):
      heap  = n * log2(n + 1)                 (pop/push driven, O(n log n))
      wheel = max(1, span / tick_ms) + n      (slot scans + bucketing)
    The wheel wins only when n crosses ``switch_threshold`` AND its estimated
    cost is strictly lower; switching back uses symmetric rules with a
    hysteresis margin so the mode does not flap.
    """

    enabled: bool = True
    switch_threshold: int = 64
    hysteresis_ratio: float = 0.8


@dataclass(frozen=True)
class QueueConfig:
    rollback_tolerance_ms: int = 0
    lease_timeout_ms: int = 5000
    wheel: WheelConfig = field(default_factory=WheelConfig)
    adaptive: AdaptiveConfig = field(default_factory=AdaptiveConfig)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        data = dict(data or {})
        wheel = WheelConfig(**data.pop("wheel", {}))
        adaptive = AdaptiveConfig(**data.pop("adaptive", {}))
        return cls(wheel=wheel.validate(), adaptive=adaptive, **data)
