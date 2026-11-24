"""Exact decimal parsing and integer fixed-point arithmetic.

The engine uses milliseconds as the logical unit.  ``FIXED_SCALE=1000`` means
one fixed-point unit is one microsecond.  Rates use the same scale, so a rate
in ``[0, 1]`` is represented in ``[0, 1000]``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

FIXED_SCALE_VERSION = "fp-q1000-v1"
FIXED_SCALE_VERSION_NUMBER = 1
FIXED_SCALE = 1000
CHECKPOINT_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1
SAMPLE_SCHEMA_VERSION = 1


class FixedPointError(ValueError):
    """Raised when a JSON value cannot become a bounded fixed-point value."""


def to_decimal(value: Any, field_name: str = "value") -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise FixedPointError(f"{field_name} must be numeric, not boolean")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # JSON numbers have already had their shortest decimal representation
        # recovered by using parse_float=Decimal in the loader.  This branch is
        # for programmatically constructed samples.
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as exc:
            raise FixedPointError(f"{field_name} is not a valid decimal") from exc
    raise FixedPointError(f"{field_name} must be numeric")


def parse_fixed(
    value: Any,
    *,
    field_name: str = "value",
    scale: int = FIXED_SCALE,
    minimum: Decimal | int | None = None,
    maximum: Decimal | int | None = None,
    strict_range: bool = True,
) -> int:
    """Parse a number as rounded fixed-point units while checking its decimal range."""
    decimal_value = to_decimal(value, field_name)
    if not decimal_value.is_finite():
        raise FixedPointError(f"{field_name} must be finite")
    if strict_range:
        if minimum is not None and decimal_value < Decimal(minimum):
            raise FixedPointError(f"{field_name} must be >= {minimum}")
        if maximum is not None and decimal_value > Decimal(maximum):
            raise FixedPointError(f"{field_name} must be <= {maximum}")
    scaled = decimal_value * Decimal(scale)
    return int(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_nonnegative_fixed(value: Any, field_name: str, scale: int = FIXED_SCALE) -> int:
    return parse_fixed(value, field_name=field_name, scale=scale, minimum=0)


def parse_positive_fixed(value: Any, field_name: str, scale: int = FIXED_SCALE) -> int:
    value_q = parse_fixed(value, field_name=field_name, scale=scale)
    if value_q <= 0:
        raise FixedPointError(f"{field_name} must be positive")
    return value_q


def parse_unit_interval(value: Any, field_name: str, scale: int = FIXED_SCALE) -> int:
    return parse_fixed(
        value,
        field_name=field_name,
        scale=scale,
        minimum=0,
        maximum=1,
    )


def q_to_float(value_q: int, scale: int = FIXED_SCALE) -> float:
    return value_q / scale


def q_to_ms(value_q: int, scale: int = FIXED_SCALE) -> float:
    return value_q / scale


def mul_floor(left_q: int, right_q: int, scale: int = FIXED_SCALE) -> int:
    """Floor ``left * right`` where both values use ``scale``."""
    return (left_q * right_q) // scale


def div_floor(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise FixedPointError("denominator must be positive")
    return numerator // denominator


@dataclass(frozen=True)
class ComponentEstimate:
    key: str
    fixed_q: int
    reference: float
    error_bound_q: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixed_q": self.fixed_q,
            "fixed_ms": q_to_float(self.fixed_q),
            "reference_ms": self.reference,
            "error_bound_q": self.error_bound_q,
            "error_bound_ms": q_to_float(self.error_bound_q),
            "residual_ms": self.reference - q_to_float(self.fixed_q),
        }


def integer_field(value: Any, field_name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FixedPointError(f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise FixedPointError(f"{field_name} must be >= {minimum}")
    return value
