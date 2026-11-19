from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any


ROUNDING_MODES = frozenset({"floor", "ceil", "nearest"})


def round_ratio(numerator: int, denominator: int, mode: str) -> tuple[int, dict[str, Any]]:
    """Round an integer ratio and retain a complete, deterministic audit record."""

    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if mode not in ROUNDING_MODES:
        raise ValueError(f"unknown rounding mode: {mode}")

    quotient, remainder = divmod(numerator, denominator)
    if remainder == 0:
        rounded = quotient
        direction = "exact"
    elif mode == "floor":
        rounded = quotient
        direction = "down"
    elif mode == "ceil":
        rounded = quotient + 1
        direction = "up"
    else:
        rounded = (numerator + denominator // 2) // denominator
        direction = "up" if rounded > quotient else "down"

    return rounded, {
        "mode": mode,
        "numerator": numerator,
        "denominator": denominator,
        "quotient": quotient,
        "remainder": remainder,
        "direction": direction,
    }


def format_fixed(value: int, scale: int) -> str:
    """Render a fixed-point integer exactly, without binary floating point."""

    if scale <= 0:
        raise ValueError("scale must be positive")
    sign = "-" if value < 0 else ""
    whole, remainder = divmod(abs(value), scale)
    if remainder == 0:
        return f"{sign}{whole}"
    with localcontext() as context:
        context.prec = 80
        fraction = Decimal(remainder) / Decimal(scale)
        text = format(fraction, "f")
    if text.startswith("0."):
        text = text[1:]
    return f"{sign}{whole}{text}"


def to_fixed(byte_count: int, scale: int) -> int:
    return byte_count * scale

