from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MarginResult:
    lag_bytes: int
    replay_rate_bytes_per_sec: int
    scale_units_per_ms: int
    declared_min_units: int
    carry_in_units: int
    numerator: int
    computed_units: int
    remainder_units: int
    effective_units: int
    precision_sensitive: bool

    @property
    def margin_ms(self) -> int:
        return self.effective_units // self.scale_units_per_ms

    def as_dict(self) -> dict[str, int | bool | str]:
        return {
            "formula": "floor((lag_bytes * 1000 * scale_units_per_ms + carry_in_units) / replay_rate_bytes_per_sec)",
            "lag_bytes": self.lag_bytes,
            "replay_rate_bytes_per_sec": self.replay_rate_bytes_per_sec,
            "scale_units_per_ms": self.scale_units_per_ms,
            "declared_min_units": self.declared_min_units,
            "carry_in_units": self.carry_in_units,
            "numerator": self.numerator,
            "computed_margin_units": self.computed_units,
            "discarded_remainder_units": self.remainder_units,
            "effective_margin_units": self.effective_units,
            "effective_margin_ms": self.margin_ms,
            "rounding_direction": "floor",
            "precision_sensitive": self.precision_sensitive,
        }


def compute_margin(
    *,
    lag_bytes: int,
    replay_rate_bytes_per_sec: int,
    scale_units_per_ms: int,
    declared_min_units: int,
    carry_in_units: int = 0,
) -> MarginResult:
    if lag_bytes < 0:
        raise ValueError("lag_bytes must be non-negative")
    if replay_rate_bytes_per_sec <= 0:
        raise ValueError("replay_rate_bytes_per_sec must be positive")
    if scale_units_per_ms <= 0:
        raise ValueError("scale_units_per_ms must be positive")
    if declared_min_units < 0 or carry_in_units < 0:
        raise ValueError("fixed-point values must be non-negative")
    numerator = lag_bytes * 1000 * scale_units_per_ms + carry_in_units
    computed_units, remainder = divmod(numerator, replay_rate_bytes_per_sec)
    effective_units = max(computed_units, declared_min_units)
    precision_sensitive = remainder != 0 or effective_units % scale_units_per_ms != 0
    return MarginResult(
        lag_bytes=lag_bytes,
        replay_rate_bytes_per_sec=replay_rate_bytes_per_sec,
        scale_units_per_ms=scale_units_per_ms,
        declared_min_units=declared_min_units,
        carry_in_units=carry_in_units,
        numerator=numerator,
        computed_units=computed_units,
        remainder_units=remainder,
        effective_units=effective_units,
        precision_sensitive=precision_sensitive,
    )
