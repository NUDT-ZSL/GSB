"""Exact decimal and vectorized controlled fixed-point conversion."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

import numpy as np

from .errors import ControlPlaneError

MAX_SAFE_INT64 = np.iinfo(np.int64).max


def as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ControlPlaneError("E_INVALID_TYPE", "boolean is not a numeric value")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ControlPlaneError("E_INVALID_NUMBER", "number must be finite")
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ControlPlaneError(
                "E_INVALID_NUMBER", f"invalid decimal literal: {value!r}"
            ) from exc
    raise ControlPlaneError(
        "E_INVALID_TYPE", f"expected a number, got {type(value).__name__}"
    )


def decimal_raw(value: Any) -> float:
    result = float(as_decimal(value))
    if not math.isfinite(result):
        raise ControlPlaneError("E_INVALID_NUMBER", "number is outside float range")
    return result


def decimal_parts(value: Any) -> tuple[int, int]:
    """Return a positive numerator and denominator for a decimal value."""

    dec = as_decimal(value)
    if not dec.is_finite():
        raise ControlPlaneError("E_INVALID_NUMBER", "number must be finite")
    sign, digits, exponent = dec.as_tuple()
    if abs(exponent) > 18:
        raise ControlPlaneError(
            "E_FIXED_RANGE",
            "decimal exponent is outside the controlled 64-bit fixed-point range",
        )
    numerator = 0
    for digit in digits:
        numerator = numerator * 10 + digit
    if sign:
        numerator = -numerator
    if exponent >= 0:
        return numerator * (10**exponent), 1
    return numerator, 10 ** (-exponent)


@dataclass(frozen=True)
class FixedBatch:
    fixed: np.ndarray
    rounding: tuple[str, ...]
    quantization_error: tuple[float, ...]
    residual_ticks: tuple[float, ...]


def convert_values(values: Sequence[Any], scale: int) -> FixedBatch:
    """Convert decimal values to fixed point using vectorized half-even rounding.

    The conversion is exact before rounding.  A value is represented as
    ``numerator / denominator``; multiplying by the integer scale keeps all
    rounding decisions in integer arithmetic and therefore independent of
    binary floating-point representation.
    """

    if type(scale) is not int or scale <= 0:
        raise ControlPlaneError("E_INVALID_SCALE", "fixed-point scale must be positive")
    if not values:
        empty = np.asarray([], dtype=np.int64)
        return FixedBatch(empty, (), (), ())

    numerators: list[int] = []
    denominators: list[int] = []
    for value in values:
        numerator, denominator = decimal_parts(value)
        if numerator > MAX_SAFE_INT64 // scale:
            raise ControlPlaneError(
                "E_FIXED_RANGE",
                "value times fixed-point scale exceeds signed 64-bit range",
            )
        numerators.append(numerator)
        denominators.append(denominator)

    nums = np.asarray(numerators, dtype=np.int64)
    dens = np.asarray(denominators, dtype=np.int64)
    scaled_nums = nums * np.int64(scale)
    quotients, remainders = np.divmod(scaled_nums, dens)

    half = dens // np.int64(2)
    tie = ((dens % np.int64(2)) == 0) & (remainders == half)
    round_up = (remainders > half) | (tie & ((quotients & 1) == 1))
    fixed = quotients + round_up.astype(np.int64)

    rounding: list[str] = []
    errors: list[float] = []
    residual_ticks: list[float] = []
    for index in range(len(fixed)):
        # Positive means the fixed result is above the exact decimal value.
        residual_value = (
            int(fixed[index]) * int(dens[index])
            - int(scaled_nums[index])
        )
        if residual_value > 0:
            rounding.append("up")
        elif residual_value < 0:
            rounding.append("down")
        else:
            rounding.append("none")
        error = abs(residual_value) / (int(dens[index]) * scale)
        errors.append(float(error))
        residual_ticks.append(residual_value / int(dens[index]))

    return FixedBatch(
        fixed=fixed,
        rounding=tuple(rounding),
        quantization_error=tuple(errors),
        residual_ticks=tuple(residual_ticks),
    )


def fixed_record(value: Any, scale: int) -> tuple[dict[str, Any], int]:
    """Convert one value and return its auditable record and integer value."""

    batch = convert_values([value], scale)
    fixed = int(batch.fixed[0])
    return (
        {
            "raw": decimal_raw(value),
            "raw_decimal": _decimal_literal(value),
            "scale": scale,
            "fixed": fixed,
            "rounding": batch.rounding[0],
            "rounding_mode": "half_even",
            "quantization_error": batch.quantization_error[0],
        },
        fixed,
    )


def tolerance_ticks(tolerance: Any, scale: int) -> int:
    """Quantize a non-negative tolerance with the same controlled converter."""

    batch = convert_values([tolerance], scale)
    return int(batch.fixed[0])


def _decimal_literal(value: Any) -> str:
    dec = as_decimal(value)
    if dec == 0:
        return "0"
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def sum_error_bounds(values: Iterable[float], result_magnitude: float = 0.0) -> float:
    """Conservative error bound for an exact fixed-point aggregation."""

    bound = math.fsum(max(0.0, value) for value in values)
    bound += sys_float_epsilon() * max(1.0, abs(result_magnitude))
    return bound


def product_error_bound(
    left_value: float,
    left_error: float,
    right_value: float,
    right_error: float,
) -> float:
    bound = (
        abs(right_value) * left_error
        + abs(left_value) * right_error
        + left_error * right_error
    )
    bound += sys_float_epsilon() * max(1.0, abs(left_value * right_value))
    return bound


def sys_float_epsilon() -> float:
    import sys

    return sys.float_info.epsilon
