from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP
from typing import Any

from .errors import fail


_ROUNDING = {
    "floor": ROUND_FLOOR,
    "ceil": ROUND_CEILING,
    "half_up": ROUND_HALF_UP,
    "half_even": ROUND_HALF_EVEN,
}


def _as_decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        fail("TIME_CONVERSION_INVALID", f"{field} is not a decimal number", field=field, value=value)


def _as_integral_milliseconds(value: Any, field: str) -> int:
    if isinstance(value, bool):
        fail("TIME_CONVERSION_INVALID", f"{field} must be integer milliseconds", field=field, value=value)
    number = _as_decimal(value, field)
    if number != number.to_integral_value():
        fail(
            "TIME_CONVERSION_INVALID",
            f"{field} has sub-millisecond precision",
            field=field,
            value=value,
        )
    result = int(number)
    if result < 0:
        fail("TIME_CONVERSION_INVALID", f"{field} must be non-negative", field=field, value=value)
    return result


def convert_relative_time(
    relative: Any,
    *,
    base_ms: int,
    scale_ms: Any = "1",
    rounding: str = "half_up",
) -> tuple[int, dict[str, Any]]:
    if rounding not in _ROUNDING:
        fail(
            "TIME_CONVERSION_INVALID",
            "unsupported rounding mode",
            rounding=rounding,
            supported=sorted(_ROUNDING),
        )
    base_ms = _as_integral_milliseconds(base_ms, "base_ms")
    scale = _as_decimal(scale_ms, "scale_ms")
    if scale <= 0:
        fail("TIME_CONVERSION_INVALID", "time scale must be positive", scale_ms=str(scale_ms))
    value = _as_decimal(relative, "updated_relative")
    exact = Decimal(base_ms) + value * scale
    absolute = int(exact.quantize(Decimal("1"), rounding=_ROUNDING[rounding]))
    if absolute < 0:
        fail("TIME_CONVERSION_INVALID", "converted time is negative", exact=str(exact))
    delta = exact - Decimal(absolute)
    if delta == 0:
        direction = "none"
    elif delta < 0:
        direction = "up"
    else:
        direction = "down"
    provenance = {
        "kind": "relative",
        "base_ms": base_ms,
        "relative": str(relative),
        "scale_ms": str(scale),
        "rounding": rounding,
        "exact_ms": str(exact),
        "absolute_ms": absolute,
        "rounding_delta_ms": str(delta),
        "rounding_direction": direction,
    }
    return absolute, provenance


def absolute_time_provenance(value: Any) -> tuple[int, dict[str, Any]]:
    result = _as_integral_milliseconds(value, "absolute_updated_ms")
    return result, {
        "kind": "absolute",
        "absolute_ms": result,
        "original": str(value),
        "scale_ms": "1",
        "rounding": "none",
        "rounding_direction": "none",
    }
