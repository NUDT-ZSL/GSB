"""Decimal-to-fixed-point conversion with an auditable rounding trail."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any

ROUNDING_MODE = "ROUND_HALF_EVEN"


def dec(value: Decimal | float | int | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def quantize_fixed(value: Decimal | float | int | str, scale: int) -> tuple[int, str]:
    exact = dec(value) * scale
    rounded = int(exact.to_integral_value(rounding=ROUND_HALF_EVEN))
    delta = exact - Decimal(rounded)
    if delta > 0:
        direction = "toward_zero"
    elif delta < 0:
        direction = "away_from_zero"
    else:
        direction = "none"
    return rounded, direction


def fixed_record(
    value: Decimal | float | int | str,
    scale: int,
    *,
    quantity: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    exact = dec(value)
    fixed, direction = quantize_fixed(exact, scale)
    record: dict[str, Any] = {
        "quantity": quantity,
        "raw_float": float(exact),
        "raw_decimal": format(exact, "f"),
        "scale": scale,
        "fixed": fixed,
        "rounding": ROUNDING_MODE,
        "rounding_direction": direction,
    }
    if extra:
        record.update(extra)
    return record


def fixed_to_decimal(fixed: int, scale: int) -> Decimal:
    return Decimal(fixed) / Decimal(scale)


def canonical_decimal(value: Decimal | float | int | str) -> str:
    number = dec(value)
    text = format(number.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def ceil_decimal(value: Decimal) -> int:
    integral = int(value)
    return integral if value == integral else integral + 1

