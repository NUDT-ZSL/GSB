"""Exact rational arithmetic and explicitly bounded fixed-point output.

All ratio and burn-rate calculations remain :class:`fractions.Fraction` until
the public output boundary.  A value is quantized once, with a deterministic
round-half-even rule.  Every serialized value carries both the integer ticks and
an upper bound (in ticks) for the absolute quantization error.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .errors import ConfigError, SnapshotError


def _require_power_of_ten(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    text = str(value)
    if text[0] != "1" or any(char != "0" for char in text[1:]):
        raise ConfigError(f"{name} must be a power of ten")


def format_scaled_int(ticks: int, scale: int) -> str:
    """Render ``ticks / scale`` without converting through binary float."""

    sign = "-" if ticks < 0 else ""
    whole, fraction = divmod(abs(ticks), scale)
    places = len(str(scale)) - 1
    return f"{sign}{whole}.{fraction:0{places}d}"


@dataclass(frozen=True, slots=True)
class FixedPoint:
    """A value represented as integer ticks at a power-of-ten scale.

    ``error_upper_ticks`` is an integer upper bound on ``abs(exact - ticks)``.
    A conservative bound of one tick is used whenever rounding is not exact.
    """

    ticks: int | None
    scale: int
    error_upper_ticks: int | None

    def __post_init__(self) -> None:
        _require_power_of_ten(self.scale, "scale")
        if (self.ticks is None) != (self.error_upper_ticks is None):
            raise ValueError("ticks and error_upper_ticks must both be defined or both be None")
        if self.ticks is not None:
            if isinstance(self.ticks, bool) or not isinstance(self.ticks, int):
                raise ValueError("ticks must be an integer")
            if (
                isinstance(self.error_upper_ticks, bool)
                or not isinstance(self.error_upper_ticks, int)
                or self.error_upper_ticks < 0
            ):
                raise ValueError("error_upper_ticks must be a non-negative integer")

    @property
    def defined(self) -> bool:
        return self.ticks is not None

    @property
    def value(self) -> str | None:
        if self.ticks is None:
            return None
        return format_scaled_int(self.ticks, self.scale)

    @property
    def error_upper(self) -> str | None:
        if self.error_upper_ticks is None:
            return None
        return format_scaled_int(self.error_upper_ticks, self.scale)

    def to_fraction(self) -> Fraction | None:
        if self.ticks is None:
            return None
        return Fraction(self.ticks, self.scale)

    def to_dict(self) -> dict[str, Any]:
        return {
            "defined": self.defined,
            "ticks": self.ticks,
            "scale": self.scale,
            "value": self.value,
            "error_upper_ticks": self.error_upper_ticks,
            "error_upper": self.error_upper,
        }

    @classmethod
    def undefined(cls, scale: int) -> "FixedPoint":
        return cls(ticks=None, scale=scale, error_upper_ticks=None)

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, context: str = "fixed point") -> "FixedPoint":
        if not isinstance(payload, dict):
            raise SnapshotError(f"{context} must be an object")
        try:
            result = cls(
                ticks=payload.get("ticks"),
                scale=payload.get("scale"),
                error_upper_ticks=payload.get("error_upper_ticks"),
            )
        except (TypeError, ValueError, ConfigError) as exc:
            raise SnapshotError(f"invalid {context}: {exc}") from exc
        if payload.get("defined", result.defined) != result.defined:
            raise SnapshotError(f"invalid {context}: defined flag does not match ticks")
        if payload.get("value") != result.value or payload.get("error_upper") != result.error_upper:
            raise SnapshotError(f"invalid {context}: rendered decimal does not match ticks")
        return result


def quantize(value: Fraction, scale: int) -> FixedPoint:
    """Quantize an exact rational to ``scale`` using deterministic half-even.

    The reported error bound is the integer ceiling of the absolute error in
    ticks.  It is therefore 0 for exactly representable values and 1 otherwise.
    """

    _require_power_of_ten(scale, "scale")
    if not isinstance(value, Fraction):
        value = Fraction(value)

    numerator = value.numerator * scale
    denominator = value.denominator
    sign = -1 if numerator < 0 else 1
    quotient, remainder = divmod(abs(numerator), denominator)
    doubled = remainder * 2
    if doubled > denominator or (doubled == denominator and quotient % 2 == 1):
        quotient += 1
    error_bound = 0 if remainder == 0 else 1
    return FixedPoint(sign * quotient, scale, error_bound)


def quantize_int(value: int, scale: int) -> FixedPoint:
    return quantize(Fraction(value, 1), scale)


def ppm_fraction(ppm: int) -> Fraction:
    return Fraction(ppm, 1_000_000)

