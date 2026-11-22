"""Deterministic fixed-point conversion helpers."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class ConversionResult:
    rounded_units: int
    residue_before: Fraction
    residue_after: Fraction
    discarded: Fraction
    exact: Fraction
    exact_num: int
    exact_den: int
    rounding: str = "floor"


def convert_fraction(exact: Fraction, residue: Fraction) -> ConversionResult:
    """Round an exact quantity to whole fixed units using a carried residue.

    The conversion is always floor.  The fractional part, including the
    previously carried residue, is retained for the next conversion on the
    same queue/direction.
    """

    if exact < 0 or residue < 0:
        raise ValueError("fixed-point conversion quantities must be non-negative")
    total = exact + residue
    units = total.numerator // total.denominator
    residue_after = total - units
    return ConversionResult(
        rounded_units=units,
        residue_before=residue,
        residue_after=residue_after,
        discarded=residue_after,
        exact=exact,
        exact_num=exact.numerator,
        exact_den=exact.denominator,
    )


def usecs_to_ticks(usecs: int, scale: int) -> int:
    return usecs * scale


def frames_to_ticks(
    frames: int,
    avg_frame_interval_us: Fraction,
    scale: int,
    residue: Fraction,
) -> ConversionResult:
    exact = Fraction(frames) * avg_frame_interval_us * scale
    return convert_fraction(exact, residue)


def fraction_json(value: Fraction) -> dict[str, str | int]:
    return {
        "decimal": fraction_decimal(value),
        "den": value.denominator,
        "num": value.numerator,
    }


def fraction_decimal(value: Fraction, precision: int = 18) -> str:
    """Render a fraction deterministically without using binary floats."""

    sign = "-" if value < 0 else ""
    value = abs(value)
    whole, remainder = divmod(value.numerator, value.denominator)
    digits: list[str] = []
    for _ in range(precision):
        remainder *= 10
        digit, remainder = divmod(remainder, value.denominator)
        digits.append(str(digit))
        if remainder == 0:
            break
    if not digits:
        return f"{sign}{whole}"
    return f"{sign}{whole}." + "".join(digits)


def float_decimal(value: float, precision: int = 18) -> str:
    return format(value, f".{precision}g")

