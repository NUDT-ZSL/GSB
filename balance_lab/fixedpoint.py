from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

import numpy as np

from .errors import ValidationError

DEFAULT_SCALE = 1_000_000


def quantize(
    value: Any,
    scale: int = DEFAULT_SCALE,
    *,
    field: str = "value",
    code: str = "E_FIXED_NOT_QUANTIZABLE",
) -> int:
    """Quantize a JSON number/string as ``round(value * scale)`` exactly.

    Inputs with more fractional digits than the scale can represent are
    rejected instead of silently changing the configuration. Decimal is used to
    avoid binary floating-point parsing ambiguity.
    """

    if isinstance(value, bool) or value is None:
        raise ValidationError(
            f"{field} must be an integer or finite decimal number",
            code=code,
            details={"field": field, "value": repr(value)},
        )
    if isinstance(value, int):
        decimal_value = Decimal(value)
    elif isinstance(value, str):
        try:
            decimal_value = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValidationError(
                f"{field} is not a valid decimal",
                code=code,
                details={"field": field, "value": value},
            ) from exc
    elif isinstance(value, float):
        if not np.isfinite(value):
            raise ValidationError(
                f"{field} must be finite",
                code=code,
                details={"field": field, "value": value},
            )
        decimal_value = Decimal(str(value))
    else:
        raise ValidationError(
            f"{field} has unsupported numeric type {type(value).__name__}",
            code=code,
            details={"field": field, "type": type(value).__name__},
        )

    if not decimal_value.is_finite():
        raise ValidationError(
            f"{field} must be finite",
            code=code,
            details={"field": field, "value": str(decimal_value)},
        )

    scaled = decimal_value * Decimal(scale)
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise ValidationError(
            f"{field}={value!r} is not exactly representable at scale {scale}",
            code=code,
            details={
                "field": field,
                "value": value,
                "scale": scale,
                "scaled": str(scaled),
            },
        )
    return int(integral)


def round_div(numerator: int, denominator: int) -> int:
    """Half-up rounded integer division for signed values."""

    if denominator == 0:
        raise ZeroDivisionError("fixed-point division by zero")
    if denominator < 0:
        numerator = -numerator
        denominator = -denominator
    if numerator >= 0:
        return (numerator + denominator // 2) // denominator
    return -((-numerator + denominator // 2) // denominator)


def fixed_mul(left: int, right: int, scale: int = DEFAULT_SCALE) -> int:
    return round_div(left * right, scale)


def fixed_ratio(numerator: int, denominator: int) -> int:
    """Return numerator/denominator as a raw fixed-point value."""

    return round_div(numerator * DEFAULT_SCALE, denominator)


def fixed_sum(values: Iterable[int]) -> int:
    values = list(values)
    if not values:
        return 0
    array = np.asarray(values, dtype=np.int64)
    return int(array.sum(dtype=np.int64))


@dataclass(frozen=True)
class ResidualSummary:
    max_abs: float
    max_relative: float
    breach_count: int
    first_breach: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_abs": self.max_abs,
            "max_relative": self.max_relative,
            "breach_count": self.breach_count,
            "first_breach": self.first_breach,
        }


def residual_summary(
    records: list[dict[str, Any]],
    *,
    scale: int,
    abs_tolerance: float,
    relative_tolerance: float,
) -> ResidualSummary:
    """Compare raw fixed values with their float64 reference values."""

    if not records:
        return ResidualSummary(0.0, 0.0, 0, None)

    fixed = np.asarray(
        [record["fixed_raw"] for record in records], dtype=np.float64
    ) / np.float64(scale)
    reference = np.asarray(
        [record["float_value"] for record in records], dtype=np.float64
    )
    abs_diff = np.abs(fixed - reference)
    rel_diff = abs_diff / np.maximum(np.abs(reference), np.float64(1.0e-12))
    tolerance = np.float64(abs_tolerance) + np.float64(relative_tolerance) * np.abs(
        reference
    )
    breaches = abs_diff > tolerance
    first_index = int(np.argmax(breaches)) if bool(np.any(breaches)) else -1
    first: dict[str, Any] | None = None
    if first_index >= 0:
        record = records[first_index]
        first = {
            "round": record.get("round"),
            "unit_id": record.get("unit_id"),
            "skill_id": record.get("skill_id"),
            "metric": record.get("metric"),
            "fixed_value": float(fixed[first_index]),
            "float_value": float(reference[first_index]),
            "abs_diff": float(abs_diff[first_index]),
            "relative_diff": float(rel_diff[first_index]),
        }
    return ResidualSummary(
        max_abs=float(np.max(abs_diff)),
        max_relative=float(np.max(rel_diff)),
        breach_count=int(np.count_nonzero(breaches)),
        first_breach=first,
    )
