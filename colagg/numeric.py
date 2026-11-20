from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Any

from .errors import E_FIXED_OVERFLOW, EngineError


@dataclass(frozen=True, slots=True)
class FixedConversion:
    raw_repr: str
    scale: int
    scaled: int | None
    direction: str
    nonfinite: str | None


def raw_float_repr(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return float(value).hex()


def convert_fixed(
    value: float | int,
    scale: int,
    minimum: int,
    maximum: int,
) -> FixedConversion:
    raw = float(value)
    raw_repr = raw_float_repr(raw)
    if math.isnan(raw):
        return FixedConversion(raw_repr, scale, None, "nonfinite", "nan")
    if math.isinf(raw):
        direction = "positive_infinity" if raw > 0 else "negative_infinity"
        return FixedConversion(raw_repr, scale, None, "nonfinite", direction)

    with localcontext() as ctx:
        ctx.prec = 200
        if isinstance(value, int):
            exact = Decimal(value) * scale
        else:
            exact = Decimal.from_float(float(value)) * scale
        quantized = exact.quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
        scaled = int(quantized)
        if quantized == exact:
            direction = "none"
        elif quantized > exact:
            direction = "up"
        else:
            direction = "down"

    if scaled < minimum or scaled > maximum:
        raise EngineError(
            E_FIXED_OVERFLOW,
            "fixed-point conversion exceeds configured integer width",
            raw=raw_repr,
            scale=scale,
            scaled="outside_bounds",
            fixed_int_bits=None,
        )
    return FixedConversion(raw_repr, scale, scaled, direction, None)


def ensure_fixed_bounds(
    value: int,
    minimum: int,
    maximum: int,
    *,
    field: str,
    details: dict[str, Any] | None = None,
) -> int:
    if value < minimum or value > maximum:
        payload = dict(details or {})
        payload.update(
            {
                "field": field,
                "fixed_int_bits": None,
                "bound_min": minimum,
                "bound_max": maximum,
            }
        )
        raise EngineError(
            E_FIXED_OVERFLOW,
            f"fixed-point {field} exceeds configured integer width",
            **payload,
        )
    return value


def is_nonfinite(value: float) -> bool:
    return math.isnan(value) or math.isinf(value)
