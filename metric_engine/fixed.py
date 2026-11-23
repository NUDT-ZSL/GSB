from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Literal

from .errors import fail

RoundingDirection = Literal["FLOOR", "CEILING", "HALF_UP"]


@dataclass(frozen=True)
class Conversion:
    value: Decimal
    scale: int
    units: int
    direction: RoundingDirection
    discarded_remainder_units: Decimal
    error_bound_units: Decimal


@dataclass(frozen=True)
class ArithmeticResult:
    units: int
    error_bound_units: Decimal


def _rounding_mode(direction: RoundingDirection) -> str:
    if direction == "FLOOR":
        return ROUND_FLOOR
    if direction == "CEILING":
        return ROUND_CEILING
    if direction == "HALF_UP":
        return ROUND_HALF_UP
    fail("E_BAD_ROUNDING", "unsupported rounding direction", direction=direction)


def to_units(
    value: Decimal | int | str,
    scale: int,
    direction: RoundingDirection = "FLOOR",
) -> Conversion:
    if scale <= 0:
        fail("E_BAD_SCALE", "fixed-point scale must be positive", scale=scale)
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    if not decimal_value.is_finite():
        fail("E_BAD_DECIMAL", "value must be finite", value=str(value))
    exact = decimal_value * Decimal(scale)
    units = int(exact.quantize(Decimal("1"), rounding=_rounding_mode(direction)))
    remainder = exact - Decimal(units)
    return Conversion(
        value=decimal_value,
        scale=scale,
        units=units,
        direction=direction,
        discarded_remainder_units=remainder,
        error_bound_units=abs(remainder),
    )


def add_error(
    left_units: int,
    right_units: int,
    left_error: Decimal,
    right_error: Decimal,
) -> ArithmeticResult:
    return ArithmeticResult(
        units=left_units + right_units,
        error_bound_units=left_error + right_error,
    )


def multiply_error(
    units: int,
    multiplier: int,
    error: Decimal,
) -> ArithmeticResult:
    return ArithmeticResult(
        units=units * multiplier,
        error_bound_units=abs(Decimal(multiplier)) * error,
    )


def units_to_decimal(units: int, scale: int) -> Decimal:
    return Decimal(units) / Decimal(scale)


def decimal_json(value: Decimal) -> str:
    return format(value, "f")

