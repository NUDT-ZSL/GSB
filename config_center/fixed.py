from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, localcontext
from typing import Any

from .models import ConfigDecl
from .util import decimal_text


INPUT_SCALE = 1_000_000
ROUNDING_DIRECTION = "floor_toward_negative_infinity"
DECIMAL_PRECISION = 80


@dataclass(frozen=True)
class ConversionRecord:
    key: str
    scale: int
    value_text: str
    raw_scaled: Decimal
    carried_in: Decimal
    adjusted_scaled: Decimal
    fixed_int: int
    discarded: Decimal
    carried_out: Decimal
    rounding_direction: str
    conversion_error: Decimal
    residual_value: Decimal


def decimal_value(value: Decimal | int | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def fixed_decimal(fixed_int: int, scale: int) -> Decimal:
    if scale <= 0:
        raise ValueError("scale must be positive")
    return Decimal(fixed_int) / Decimal(scale)


def floor_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return numerator // denominator


def convert_decl(decl: ConfigDecl, carried_in: Decimal = Decimal(0)) -> ConversionRecord:
    if not decl.is_numeric or decl.value_decimal is None:
        raise ValueError(f"{decl.key} is not numeric")
    if carried_in < 0 or carried_in >= 1:
        raise ValueError("carried_in residual must be in [0, 1)")
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        raw_scaled = decl.value_decimal * Decimal(decl.scale)
        adjusted_scaled = raw_scaled + carried_in
        fixed_int = int(adjusted_scaled.to_integral_value(rounding=ROUND_FLOOR))
        discarded = adjusted_scaled - Decimal(fixed_int)
        conversion_error = fixed_decimal(fixed_int, decl.scale) - decl.value_decimal
        return ConversionRecord(
            key=decl.key,
            scale=decl.scale,
            value_text=decl.value_text,
            raw_scaled=raw_scaled,
            carried_in=carried_in,
            adjusted_scaled=adjusted_scaled,
            fixed_int=fixed_int,
            discarded=discarded,
            carried_out=discarded,
            rounding_direction=ROUNDING_DIRECTION,
            conversion_error=conversion_error,
            residual_value=discarded / Decimal(decl.scale),
        )


def convert_input_decimal(value: Decimal, scale: int = INPUT_SCALE) -> tuple[int, Decimal]:
    """Convert an input event value without carrying a cross-round residual."""
    with localcontext() as ctx:
        ctx.prec = DECIMAL_PRECISION
        raw = value * Decimal(scale)
        fixed_int = int(raw.to_integral_value(rounding=ROUND_FLOOR))
        residual = raw - Decimal(fixed_int)
        return fixed_int, residual / Decimal(scale)


def conversion_to_dict(record: ConversionRecord, conversion_id: int, version: int) -> dict[str, Any]:
    return {
        "conversion_id": conversion_id,
        "namespace_version": version,
        "config_key": record.key,
        "scale": record.scale,
        "value": record.value_text,
        "raw_scaled": decimal_text(record.raw_scaled),
        "carried_in": decimal_text(record.carried_in),
        "adjusted_scaled": decimal_text(record.adjusted_scaled),
        "fixed_int": str(record.fixed_int),
        "discarded": decimal_text(record.discarded),
        "carried_out": decimal_text(record.carried_out),
        "rounding_direction": record.rounding_direction,
        "conversion_error": decimal_text(record.conversion_error),
        "residual": decimal_text(record.residual_value),
    }


def stable_decimal(value: Decimal, *, digits: int = 24) -> str:
    with localcontext() as ctx:
        ctx.prec = max(digits, 24)
        if value == 0:
            return "0"
        text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def decimal_abs(value: Decimal) -> Decimal:
    return -value if value < 0 else value

