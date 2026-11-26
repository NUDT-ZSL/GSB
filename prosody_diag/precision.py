from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import resolve_precision_profile


def _round_fixed(numerator: int, denominator: int, rounding: str) -> int:
    """Round numerator / denominator to an integer with declared sign handling."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    sign = -1 if numerator < 0 else 1
    magnitude = abs(numerator)
    if rounding == "truncate":
        return sign * (magnitude // denominator)
    quotient, remainder = divmod(magnitude, denominator)
    twice = remainder * 2
    if twice < denominator:
        rounded = quotient
    elif twice > denominator:
        rounded = quotient + 1
    elif rounding == "nearest_away":
        rounded = quotient + 1
    elif rounding == "nearest_even":
        rounded = quotient + (quotient & 1)
    else:
        raise ValueError(f"unsupported rounding mode: {rounding}")
    return sign * rounded


@dataclass
class PrecisionContext:
    """Common arithmetic semantics for float64, float32 and scaled integers."""

    profile: dict[str, Any]

    def __init__(self, profile: str | dict[str, Any]):
        self.profile = resolve_precision_profile(profile)
        self.name = self.profile["name"]
        self.mode = self.profile["mode"]
        self.scale = self.profile.get("scale")
        self.rounding = self.profile.get("rounding")

    @classmethod
    def named(cls, name: str) -> "PrecisionContext":
        return cls(name)

    def put(self, value: float | int | np.floating[Any]) -> Any:
        value = float(value)
        if self.mode == "float64":
            return np.float64(value)
        if self.mode == "float32":
            return np.float32(value)
        if self.mode == "fixed":
            scale = int(self.scale)
            scaled_twice = math.floor(value * scale * 2.0)
            return _round_fixed(scaled_twice, 2, self.rounding or "nearest_even")
        raise ValueError(f"unsupported precision mode: {self.mode}")

    def to_float(self, value: Any) -> float:
        if self.mode == "fixed":
            return float(int(value)) / float(self.scale)
        return float(value)

    def add(self, left: Any, right: Any) -> Any:
        if self.mode == "float64":
            return np.float64(np.float64(left) + np.float64(right))
        if self.mode == "float32":
            return np.float32(np.float32(left) + np.float32(right))
        return int(left) + int(right)

    def sub(self, left: Any, right: Any) -> Any:
        if self.mode == "float64":
            return np.float64(np.float64(left) - np.float64(right))
        if self.mode == "float32":
            return np.float32(np.float32(left) - np.float32(right))
        return int(left) - int(right)

    def mul(self, left: Any, right: Any) -> Any:
        if self.mode == "float64":
            return np.float64(np.float64(left) * np.float64(right))
        if self.mode == "float32":
            return np.float32(np.float32(left) * np.float32(right))
        scale = int(self.scale)
        return _round_fixed(int(left) * int(right), scale, self.rounding or "nearest_even")

    def div(self, left: Any, right: Any) -> Any:
        if self.mode == "float64":
            return np.float64(np.float64(left) / np.float64(right))
        if self.mode == "float32":
            return np.float32(np.float32(left) / np.float32(right))
        scale = int(self.scale)
        return _round_fixed(int(left) * scale, int(right), self.rounding or "nearest_even")

    def sqrt(self, value: Any) -> Any:
        if self.mode == "float64":
            return np.float64(math.sqrt(max(0.0, float(value))))
        if self.mode == "float32":
            return np.float32(math.sqrt(max(0.0, float(value))))
        integer = max(0, int(value))
        scale = int(self.scale)
        scaled_square = integer * scale
        root = math.isqrt(scaled_square)
        correction = root * root
        next_square = (root + 1) * (root + 1)
        if next_square - scaled_square < scaled_square - correction:
            root += 1
        return root

    def ge(self, left: Any, right: Any) -> bool:
        return bool(left >= right)

    def gt(self, left: Any, right: Any) -> bool:
        return bool(left > right)

    def lt(self, left: Any, right: Any) -> bool:
        return bool(left < right)

    def le(self, left: Any, right: Any) -> bool:
        return bool(left <= right)

    def spec_hash_object(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "scale": self.scale,
            "rounding": self.rounding,
        }
