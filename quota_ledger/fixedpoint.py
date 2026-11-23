from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math
from typing import Any

from .errors import error


@dataclass(frozen=True)
class Conversion:
    field: str
    scale: int
    rounding: str
    raw_numerator: int
    raw_denominator: int
    converted: int
    error_bound_micro: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "scale": self.scale,
            "rounding": self.rounding,
            "raw_numerator": self.raw_numerator,
            "raw_denominator": self.raw_denominator,
            "converted": self.converted,
            "error_bound_micro": self.error_bound_micro,
        }


def _micro_error_upper_bound(error_units: Fraction) -> int:
    """Express a fixed-point error in millionths of one fixed-point unit."""

    if error_units <= 0:
        return 0
    return math.ceil(error_units * 1_000_000)


def quantize(
    field: str,
    numerator: int,
    denominator: int,
    scale: int,
    *,
    rounding: str = "DOWN",
) -> Conversion:
    """Convert numerator/denominator to an integer at a declared fixed scale.

    The integer ledger itself stays exact. This conversion is only for the
    externally visible fixed-point view (remaining quota, borrow balance and
    usage rate), and records the rounding error upper bound.
    """

    if denominator == 0:
        raise error("FIXED_POINT_ZERO_DENOMINATOR", "定点换算分母不能为零")
    if scale <= 0:
        raise error("FIXED_POINT_INVALID_SCALE", "定点标度必须为正整数")
    if rounding not in {"DOWN", "UP", "NEAREST"}:
        raise error(
            "FIXED_POINT_INVALID_ROUNDING",
            "定点舍入方向必须为 DOWN、UP 或 NEAREST",
        )

    exact = Fraction(numerator * scale, denominator)
    floor_value = exact.numerator // exact.denominator
    if rounding == "DOWN":
        converted = floor_value
    elif rounding == "UP":
        converted = floor_value if exact.denominator == 1 else floor_value + 1
    else:
        # Explicit half-up; avoids Python's binary float banker's rounding.
        converted = math.floor(exact + Fraction(1, 2))

    error_units = abs(exact - converted)
    return Conversion(
        field=field,
        scale=scale,
        rounding=rounding,
        raw_numerator=numerator,
        raw_denominator=denominator,
        converted=converted,
        error_bound_micro=_micro_error_upper_bound(error_units),
    )


def exact_scaled(field: str, value: int, scale: int) -> Conversion:
    return Conversion(
        field=field,
        scale=scale,
        rounding="EXACT",
        raw_numerator=value,
        raw_denominator=1,
        converted=value * scale,
        error_bound_micro=0,
    )


def tolerance_micro(tolerance: float) -> int:
    return math.ceil(Fraction(str(tolerance)) * 1_000_000)
