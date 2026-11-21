from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from .errors import fail

SCALE = 1_000_000
SCALE_DECIMAL = Decimal(SCALE)
QUANTUM = Decimal("0.000001")
CORE_UNITS_PER_CORE_HOUR = 3_600_000_000


def as_decimal(
    value: Any,
    field: str,
    *,
    allow_zero: bool = True,
) -> Decimal:
    if isinstance(value, bool) or value is None:
        fail(
            "E_INVALID_NUMBER",
            f"{field} must be a finite decimal number",
            {"field": field, "value": value},
        )
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        fail(
            "E_INVALID_NUMBER",
            f"{field} must be a finite decimal number",
            {"field": field, "value": value},
        )
    if not number.is_finite():
        fail(
            "E_INVALID_NUMBER",
            f"{field} must be finite",
            {"field": field, "value": value},
        )
    if allow_zero and number == 0:
        return number
    if number <= 0:
        fail(
            "E_INVALID_NUMBER",
            f"{field} must be greater than zero",
            {"field": field, "value": str(number)},
        )
    return number


def as_int(
    value: Any,
    field: str,
    *,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(
            "E_INVALID_INTEGER",
            f"{field} must be an integer",
            {"field": field, "value": value},
        )
    if minimum is not None and value < minimum:
        fail(
            "E_INVALID_INTEGER",
            f"{field} must be at least {minimum}",
            {"field": field, "value": value, "minimum": minimum},
        )
    return value


def to_fixed(value: Any, field: str = "value", *, non_negative: bool = False) -> int:
    number = as_decimal(value, field)
    if non_negative and number < 0:
        fail(
            "E_INVALID_NUMBER",
            f"{field} must not be negative",
            {"field": field, "value": str(number)},
        )
    quantized = number.quantize(QUANTUM, rounding=ROUND_HALF_UP)
    return int(quantized * SCALE)


def fixed_text(value: int) -> str:
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    whole, fraction = divmod(absolute, SCALE)
    return f"{sign}{whole}.{fraction:06d}"


def fixed_to_decimal(value: int) -> Decimal:
    return Decimal(value) / SCALE_DECIMAL


def core_hours_to_units(value: Any, field: str = "poolCoreHours") -> int:
    number = as_decimal(value, field)
    scaled = number * Decimal(CORE_UNITS_PER_CORE_HOUR)
    integral = scaled.to_integral_value()
    if scaled != integral:
        fail(
            "E_INVALID_BUDGET",
            f"{field} is below the supported accounting resolution",
            {"field": field, "value": str(number)},
        )
    return int(integral)


def core_units_to_hours_text(value: int) -> str:
    number = Decimal(value) / Decimal(CORE_UNITS_PER_CORE_HOUR)
    text = format(number.quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if numerator <= 0:
        return 0
    return (numerator + denominator - 1) // denominator


def quantize_decimal(value: Decimal) -> Decimal:
    return value.quantize(QUANTUM, rounding=ROUND_HALF_UP)


def quantize_float(value: float) -> Decimal:
    if not math.isfinite(value):
        fail("E_INVALID_AGGREGATION", "float aggregation produced a non-finite value")
    return Decimal(str(value)).quantize(QUANTUM, rounding=ROUND_HALF_UP)

