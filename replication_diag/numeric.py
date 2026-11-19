"""Exact rational rounding used by v1 migration."""

from __future__ import annotations

import math
from fractions import Fraction


def round_fraction(value: Fraction, mode: str) -> tuple[int, str]:
    """Round ``value`` and return ``(integer, direction_description)``."""

    floor_value = value.numerator // value.denominator
    remainder = value - floor_value
    if remainder == 0:
        return floor_value, "exact"

    if mode == "floor":
        return floor_value, "down"
    if mode == "ceil":
        return floor_value + 1, "up"
    if mode == "toward_zero":
        return (floor_value if value >= 0 else floor_value + 1), "toward_zero"
    if mode == "away_from_zero":
        return (floor_value + 1 if value >= 0 else floor_value), "away_from_zero"

    if mode == "half_up":
        if remainder < Fraction(1, 2):
            return floor_value, "down"
        return floor_value + 1, "up"
    if mode == "half_even":
        if remainder < Fraction(1, 2):
            return floor_value, "down"
        if remainder > Fraction(1, 2):
            return floor_value + 1, "up"
        return (floor_value if floor_value % 2 == 0 else floor_value + 1), "tie_to_even"
    raise ValueError(f"unsupported rounding mode: {mode}")


def ceil_div(numerator: int, denominator: int) -> int:
    return -((-numerator) // denominator)


def is_finite_fraction(value: Fraction) -> bool:
    return math.isfinite(float(value))
