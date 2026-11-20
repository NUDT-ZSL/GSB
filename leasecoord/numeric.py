from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from .errors import fail


def _decimal_fraction(value: Any) -> tuple[Decimal, Fraction]:
    if isinstance(value, bool):
        fail("E_BAD_NUMBER", "Boolean is not a numeric value", details={"value": value})
    try:
        if isinstance(value, Decimal):
            decimal = value
        elif isinstance(value, Fraction):
            decimal = Decimal(value.numerator) / Decimal(value.denominator)
        elif isinstance(value, float):
            decimal = Decimal(str(value))
        else:
            decimal = Decimal(str(value))
    except (InvalidOperation, ValueError, ZeroDivisionError) as exc:
        fail(
            "E_BAD_NUMBER",
            "Value is not a finite decimal number",
            details={"value": str(value), "reason": str(exc)},
        )
        raise AssertionError("unreachable")
    if not decimal.is_finite():
        fail("E_BAD_NUMBER", "Value must be finite", details={"value": str(value)})
    return decimal, Fraction(decimal)


def fraction(value: Any) -> Fraction:
    if isinstance(value, Fraction):
        return value
    return _decimal_fraction(value)[1]


def decimal_text(value: Any) -> str:
    decimal, _ = _decimal_fraction(value)
    text = format(decimal, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"-0", ""}:
        return "0"
    return text


def fraction_text(value: Fraction, places: int = 12) -> str:
    """Canonical decimal text for values created from finite JSON decimals."""
    if value.denominator == 1:
        return str(value.numerator)
    denominator = value.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        return f"{value.numerator}/{value.denominator}"
    width = max(twos, fives)
    if width == 0:
        return str(value.numerator // value.denominator)
    sign = "-" if value < 0 else ""
    numerator = abs(value.numerator)
    whole = numerator // value.denominator
    remainder = numerator % value.denominator
    scale = 10**width
    fractional = remainder * (scale // value.denominator)
    text = f"{sign}{whole}.{str(fractional).rjust(width, '0')}"
    text = text.rstrip("0").rstrip(".")
    return text or "0"


def fraction_parts(value: Fraction) -> dict[str, int]:
    return {"num": value.numerator, "den": value.denominator}


def fraction_from_parts(num: int, den: int) -> Fraction:
    return Fraction(int(num), int(den))


@dataclass(frozen=True)
class FixedValue:
    label: str
    raw_value: str
    raw_float: float | None
    scale: int
    rounding: str
    units: int
    residual: Fraction
    value: Fraction

    @property
    def residual_units(self) -> Fraction:
        return self.residual

    @property
    def error_upper_bound_units(self) -> Fraction:
        # The residual is retained separately; the integer part alone has this bound.
        return Fraction(1, 1)

    @property
    def error_upper_bound_ms(self) -> Fraction:
        return Fraction(1, self.scale)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "raw_value": self.raw_value,
            "raw_float": self.raw_float,
            "scale": self.scale,
            "rounding": self.rounding,
            "units": self.units,
            "residual_num": self.residual.numerator,
            "residual_den": self.residual.denominator,
            "residual_units": fraction_text(self.residual),
            "value_ms": fraction_text(self.value),
            "truncation_error_upper_bound_units": "<1",
            "truncation_error_upper_bound_ms": f"<{fraction_text(self.error_upper_bound_ms)}",
            "retained_residual_comparison_error_upper_bound_units": "0",
            "retained_residual_comparison_error_upper_bound_ms": "0",
        }


def fixed_from_fraction(
    value: Fraction,
    scale: int,
    *,
    label: str,
    raw_value: str,
    raw_float: float | None = None,
) -> FixedValue:
    scaled = value * scale
    units = scaled.numerator // scaled.denominator
    residual = scaled - units
    return FixedValue(
        label=label,
        raw_value=raw_value,
        raw_float=raw_float,
        scale=scale,
        rounding="floor",
        units=units,
        residual=residual,
        value=value,
    )


def fixed_from_input(
    value: Any,
    scale: int,
    *,
    label: str,
    include_float_repr: bool = True,
) -> FixedValue:
    if isinstance(value, Fraction):
        try:
            raw_float = float(value) if include_float_repr else None
        except (OverflowError, ValueError):
            raw_float = None
        return fixed_from_fraction(
            value,
            scale,
            label=label,
            raw_value=fraction_text(value),
            raw_float=raw_float,
        )
    decimal, exact = _decimal_fraction(value)
    raw_float: float | None = None
    if include_float_repr:
        try:
            raw_float = float(decimal)
        except (OverflowError, ValueError):
            raw_float = None
    return fixed_from_fraction(
        exact,
        scale,
        label=label,
        raw_value=decimal_text(decimal),
        raw_float=raw_float,
    )


def exact_product_fixed(
    lease_ms: int,
    ratio: Fraction,
    scale: int,
    *,
    label: str = "safety_margin_ms",
) -> FixedValue:
    value = Fraction(lease_ms, 1) * ratio
    raw = fraction_text(value)
    return fixed_from_fraction(
        value,
        scale,
        label=label,
        raw_value=raw,
        raw_float=float(value),
    )


def fraction_abs(value: Fraction) -> Fraction:
    return value if value >= 0 else -value


def json_fraction(value: Fraction) -> str:
    return fraction_text(value)
