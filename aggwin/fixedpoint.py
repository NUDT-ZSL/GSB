"""Explicit fixed-point integer arithmetic for the MEAN aggregate.

The mean is never stored as a binary floating point value.  It is stored as a
scaled integer ``mean_scaled = trunc(sum / count * SCALE)`` where truncation is
always performed toward zero (the TRUNC_DIRECTION declared in the package).

Quantization error bound:
    |mean - mean_scaled / SCALE| < 1 / SCALE
    i.e. the absolute error is strictly below one unit of the last place
    (1e-4 with the default scale of 10000), for every finite group.
"""

from __future__ import annotations

from . import FIXED_POINT_SCALE, TRUNC_DIRECTION


def trunc_div(a: int, b: int) -> int:
    """Integer division truncated toward zero (Python // floors negatives)."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def mean_scaled(total: int, count: int, scale: int = FIXED_POINT_SCALE) -> int:
    if count == 0:
        raise ValueError("mean of empty group is undefined")
    return trunc_div(total * scale, count)


def quant_error_bound(scale: int = FIXED_POINT_SCALE) -> str:
    """Human-readable absolute quantization error upper bound (strict)."""
    return f"|err| < 1/{scale}"


def scaled_to_text(value_scaled: int, scale: int = FIXED_POINT_SCALE) -> str:
    """Render a scaled integer as exact decimal text."""
    sign = "-" if value_scaled < 0 else ""
    digits = str(abs(value_scaled))
    width = len(str(scale)) - 1
    if len(digits) <= width:
        digits = digits.zfill(width + 1)
    whole, frac = digits[:-width], digits[-width:]
    return f"{sign}{whole}.{frac}"


def fixed_point_spec(scale: int = FIXED_POINT_SCALE) -> dict:
    return {
        "fixed_point_version": "FP-V1",
        "scale": scale,
        "trunc_direction": TRUNC_DIRECTION,
        "quant_error_bound": quant_error_bound(scale),
    }
