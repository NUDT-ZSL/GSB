"""Exact logical-time conversion arithmetic."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .errors import fail

MAX_SCALE_COMPONENT = 1_000_000_000


def parse_fraction(value: Any, *, path: str, code: str = "E_BAD_CONFIG") -> Fraction:
    if isinstance(value, bool) or value is None:
        fail(code, "expected a finite number", path)
    if isinstance(value, int):
        return Fraction(value, 1)
    if isinstance(value, str):
        text = value.strip()
        try:
            return Fraction(text)
        except (ValueError, ZeroDivisionError):
            fail(code, f"invalid rational number: {value!r}", path)
    if isinstance(value, float):
        try:
            return Fraction(str(value))
        except (ValueError, ZeroDivisionError):
            fail(code, f"invalid numeric value: {value!r}", path)
    fail(code, "expected an integer, decimal string, or fraction string", path)


def parse_scale(value: Any, *, path: str = "config.scale") -> Fraction:
    scale = parse_fraction(value, path=path, code="E_BAD_SCALE")
    if scale <= 0:
        fail("E_BAD_SCALE", "scale must be greater than zero", path)
    if (
        scale.numerator > MAX_SCALE_COMPONENT
        or scale.denominator > MAX_SCALE_COMPONENT
    ):
        fail(
            "E_BAD_SCALE",
            f"scale components must not exceed {MAX_SCALE_COMPONENT}",
            path,
        )
    return scale


def fraction_text(value: Fraction | int) -> str:
    value = Fraction(value)
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


@dataclass(frozen=True, slots=True)
class Conversion:
    raw_ms: int
    scale: Fraction
    exact_units: Fraction
    fixed_units: int
    remainder: Fraction
    direction: str
    error_units: Fraction

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_wait_ms": self.raw_ms,
            "scale": fraction_text(self.scale),
            "exact_units": fraction_text(self.exact_units),
            "fixed_units": self.fixed_units,
            "discarded_remainder": fraction_text(self.remainder),
            "rounding_direction": self.direction,
            "signed_error_units": fraction_text(self.error_units),
        }


def convert_wait(raw_ms: int, scale: Fraction) -> Conversion:
    if raw_ms < 0:
        fail("E_INTERNAL", "wait duration cannot be negative", exit_code=4)
    exact = Fraction(raw_ms) * scale
    fixed = exact.numerator // exact.denominator
    remainder = exact - fixed
    return Conversion(
        raw_ms=raw_ms,
        scale=scale,
        exact_units=exact,
        fixed_units=fixed,
        remainder=remainder,
        direction="DOWN",
        error_units=remainder,
    )

