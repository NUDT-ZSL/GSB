from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .models import PrecisionConfig


def quantize_scalar(value: float, scale_factor: int, rounding: str) -> int:
    if not math.isfinite(value):
        raise ValueError(f"cannot quantize non-finite value: {value!r}")
    scaled = float(value) * float(scale_factor)
    if rounding == "nearest_even":
        result = round(scaled)
    elif rounding == "half_up":
        result = math.floor(scaled + 0.5) if scaled >= 0 else math.ceil(scaled - 0.5)
    elif rounding == "truncate":
        result = math.trunc(scaled)
    elif rounding == "floor":
        result = math.floor(scaled)
    elif rounding == "ceil":
        result = math.ceil(scaled)
    else:
        raise ValueError(f"unknown rounding mode: {rounding}")
    return int(result)


@dataclass(frozen=True)
class PrecisionOps:
    mode: str
    scale_factor: int = 1_000_000
    rounding: str = "nearest_even"
    subnormal_floor: float = 1e-12

    @classmethod
    def from_config(cls, config: PrecisionConfig, subnormal_floor: float) -> "PrecisionOps":
        return cls(
            mode=config.mode,
            scale_factor=config.scale_factor,
            rounding=config.rounding,
            subnormal_floor=subnormal_floor,
        )

    def coerce(self, value: float | int) -> float:
        numeric = float(value)
        if abs(numeric) < self.subnormal_floor:
            numeric = 0.0
        if self.mode == "float64":
            return float(numeric)
        if self.mode == "float32":
            return float(np.float32(numeric))
        if self.mode == "fixed":
            raw = quantize_scalar(numeric, self.scale_factor, self.rounding)
            return raw / float(self.scale_factor)
        raise ValueError(f"unknown precision mode: {self.mode}")

    def raw(self, value: float | int) -> int | None:
        if self.mode != "fixed":
            return None
        return quantize_scalar(float(value), self.scale_factor, self.rounding)

    def dequantize(self, raw: int) -> float:
        return raw / float(self.scale_factor)

    def add(self, left: float, right: float) -> float:
        return self.coerce(self.coerce(left) + self.coerce(right))

    def sub(self, left: float, right: float) -> float:
        return self.coerce(self.coerce(left) - self.coerce(right))

    def mul(self, left: float, right: float) -> float:
        return self.coerce(self.coerce(left) * self.coerce(right))

    def div(self, numerator: float, denominator: float) -> float:
        return self.coerce(self.coerce(numerator) / self.coerce(denominator))

    def div_protected(self, numerator: float, denominator: float) -> tuple[float, bool]:
        if abs(float(denominator)) < self.subnormal_floor:
            return 0.0, True
        return self.div(numerator, denominator), False

    def power(self, base: float, exponent: float) -> float:
        base_value = self.coerce(base)
        exponent_value = self.coerce(exponent)
        if base_value == 0.0 and exponent_value > 0.0:
            return 0.0
        return self.coerce(math.pow(base_value, exponent_value))

    def log(self, value: float) -> float:
        numeric = self.coerce(value)
        if numeric <= 0.0:
            raise ValueError("log domain requires a positive value")
        return self.coerce(math.log(numeric))

    def exp(self, value: float) -> float:
        return self.coerce(math.exp(self.coerce(value)))

    def sum(self, values: list[float]) -> float:
        total = self.coerce(0.0)
        for value in values:
            total = self.add(total, value)
        return total

    def record(self) -> dict[str, object]:
        if self.mode == "fixed":
            return {
                "tier": self.mode,
                "scale_factor": self.scale_factor,
                "rounding": self.rounding,
                "subnormal_floor": self.subnormal_floor,
            }
        return {
            "tier": self.mode,
            "scale_factor": None,
            "rounding": "ieee_even",
            "subnormal_floor": self.subnormal_floor,
        }


def threshold_decision(
    value: float,
    threshold: float,
    policy: str,
    tolerance: float,
) -> tuple[bool, float]:
    margin = value - threshold
    if policy == "strict":
        return value > threshold, margin
    if policy == "tolerance":
        return value >= threshold - tolerance, margin
    raise ValueError(f"unknown threshold policy: {policy}")

