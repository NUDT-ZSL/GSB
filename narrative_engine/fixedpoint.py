from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import numpy as np

from .errors import SchemaError


def as_decimal(value: Any, *, field: str = "value") -> Decimal:
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        raise SchemaError(f"{field} must be numeric, not boolean")
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise SchemaError(f"{field} is not a valid decimal: {value!r}") from exc
    else:
        raise SchemaError(f"{field} has unsupported numeric type: {type(value).__name__}")

    if not result.is_finite():
        raise SchemaError(f"{field} must be finite")
    return result


def require_finite_number(value: Any, *, field: str = "value") -> Decimal:
    decimal_value = as_decimal(value, field=field)
    if not decimal_value.is_finite():
        raise SchemaError(f"{field} must be finite")
    return decimal_value


def quantize_fixed(value: Any, scale: int, *, field: str = "value") -> int:
    if not isinstance(scale, int) or isinstance(scale, bool) or scale <= 0:
        raise SchemaError("fixed_point.scale must be a positive integer")
    decimal_value = require_finite_number(value, field=field)
    scaled = decimal_value * Decimal(scale)
    return int(scaled.to_integral_value(rounding=ROUND_HALF_UP))


def fixed_to_decimal(fixed_value: int, scale: int) -> Decimal:
    with __import__("decimal").localcontext() as context:
        context.prec = 50
        return Decimal(int(fixed_value)) / Decimal(scale)


def fixed_to_float64(fixed_value: int, scale: int) -> np.float64:
    return np.float64(fixed_value) / np.float64(scale)


def raw_to_float64(value: Any) -> np.float64:
    return np.float64(float(as_decimal(value)))


def decimal_string(value: Decimal | int) -> str:
    if isinstance(value, int):
        return str(value)
    return format(value, "f")
