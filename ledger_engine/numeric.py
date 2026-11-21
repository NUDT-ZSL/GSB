from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .jsonutil import (
    canonical_decimal,
    fraction_string,
    parse_fraction_string,
)


TEN = Fraction(10)


def scale_unit(scale: int) -> Fraction:
    return Fraction(10**scale, 1)


def floor_fraction(value: Fraction) -> int:
    return value.numerator // value.denominator


def quantize_units(value: Fraction, scale: int) -> tuple[int, Fraction]:
    raw = value * (10**scale)
    units = floor_fraction(raw)
    return units, raw - units


def direction_for(remainder: Fraction) -> str:
    return "EXACT" if remainder == 0 else "FLOOR"


@dataclass(frozen=True)
class FixedValue:
    units: int
    scale: int
    low: Fraction
    high: Fraction

    @property
    def real(self) -> Fraction:
        return Fraction(self.units, 10**self.scale)

    @property
    def bound(self) -> Fraction:
        return max(abs(self.real - self.low), abs(self.real - self.high))

    def operand(self) -> dict[str, int]:
        return {"scale": self.scale, "units": self.units}


def converted_value(
    value: Fraction,
    *,
    scale: int,
    carry_in: Fraction,
    error_budget: Fraction,
) -> tuple[FixedValue, dict[str, Any]]:
    unit = Fraction(1, 10**scale)
    raw = value * (10**scale) + carry_in
    units = floor_fraction(raw)
    remainder = raw - units
    low = value - error_budget
    if low < 0:
        low = Fraction(0)
    high = value + error_budget
    fixed = FixedValue(units=units, scale=scale, low=low, high=high)
    details = {
        "carry_in": fraction_string(carry_in),
        "carry_out": fraction_string(remainder),
        "discarded_units": fraction_string(remainder),
        "discarded_value": canonical_decimal(
            remainder / (10**scale), precision=120
        ),
        "direction": direction_for(remainder),
        "input": canonical_decimal(value),
        "unit": canonical_decimal(unit),
    }
    return fixed, details


def entry_from_operands(
    *,
    service: str,
    registration_no: int,
    registration_key: str,
    parameter: str,
    operation: str,
    weight_position: str,
    output: FixedValue,
    remainder: Fraction,
    operands: list[FixedValue],
    configured_budget: Fraction,
) -> dict[str, Any]:
    return {
        "bound_after": canonical_decimal(output.bound, precision=120),
        "configured_budget": canonical_decimal(configured_budget),
        "direction": direction_for(remainder),
        "discarded_units": fraction_string(remainder),
        "discarded_value": canonical_decimal(
            remainder / (10**output.scale), precision=120
        ),
        "operation": operation,
        "operands": [operand.operand() for operand in operands],
        "output_scale": output.scale,
        "output_units": output.units,
        "parameter": parameter,
        "registration_key": registration_key,
        "registration_no": registration_no,
        "service": service,
        "weight_position": weight_position,
    }


def fixed_from_conversion_entry(
    entry: dict[str, Any], *, error_budget: Fraction
) -> FixedValue:
    value = _fraction_from_canonical(entry["input"])
    low = value - error_budget
    if low < 0:
        low = Fraction(0)
    high = value + error_budget
    return FixedValue(
        units=int(entry["output_units"]),
        scale=int(entry["output_scale"]),
        low=low,
        high=high,
    )


def fixed_from_entry(entry: dict[str, Any]) -> FixedValue:
    return FixedValue(
        units=int(entry["output_units"]),
        scale=int(entry["output_scale"]),
        low=Fraction(0),
        high=Fraction(0),
    )


def _fraction_from_canonical(value: str) -> Fraction:
    from .jsonutil import fraction_from

    return fraction_from(value)


def add_values(
    left: FixedValue,
    right: FixedValue,
    *,
    target_scale: int,
) -> tuple[FixedValue, Fraction]:
    value = left.real + right.real
    units, remainder = quantize_units(value, target_scale)
    result = FixedValue(
        units=units,
        scale=target_scale,
        low=left.low + right.low,
        high=left.high + right.high,
    )
    return result, remainder


def multiply_values(
    left: FixedValue,
    right: FixedValue,
    *,
    target_scale: int,
) -> tuple[FixedValue, Fraction]:
    value = left.real * right.real
    units, remainder = quantize_units(value, target_scale)
    result = FixedValue(
        units=units,
        scale=target_scale,
        low=left.low * right.low,
        high=left.high * right.high,
    )
    return result, remainder


def min_values(
    left: FixedValue,
    right: FixedValue,
    *,
    target_scale: int,
) -> tuple[FixedValue, Fraction]:
    chosen = left if left.real <= right.real else right
    units, remainder = quantize_units(chosen.real, target_scale)
    result = FixedValue(
        units=units,
        scale=target_scale,
        low=min(left.low, right.low),
        high=min(left.high, right.high),
    )
    return result, remainder


def divide_values(
    numerator: FixedValue,
    denominator: FixedValue,
    *,
    target_scale: int,
) -> tuple[FixedValue, Fraction, bool]:
    if denominator.real == 0:
        result = FixedValue(
            units=0,
            scale=target_scale,
            low=Fraction(0),
            high=Fraction(0),
        )
        return result, Fraction(0), True
    value = numerator.real / denominator.real
    units, remainder = quantize_units(value, target_scale)
    if denominator.low > 0:
        low = numerator.low / denominator.high
        high = numerator.high / denominator.low
    else:
        low = numerator.low / denominator.real
        high = numerator.high / denominator.real
        if high < low:
            low, high = high, low
    result = FixedValue(
        units=units,
        scale=target_scale,
        low=low,
        high=high,
    )
    return result, remainder, False


def rescale_value(
    value: FixedValue,
    *,
    target_scale: int,
) -> tuple[FixedValue, Fraction]:
    units, remainder = quantize_units(value.real, target_scale)
    result = FixedValue(
        units=units,
        scale=target_scale,
        low=value.low,
        high=value.high,
    )
    return result, remainder


def fraction_from_entry_string(value: str) -> Fraction:
    if "/" in value:
        return parse_fraction_string(value)
    from .jsonutil import fraction_from

    return fraction_from(value)


def recompute_entry(entry: dict[str, Any]) -> tuple[int, int, Fraction]:
    """Return (output_units, output_scale, remainder) for an arithmetic entry."""

    operation = entry["operation"]
    output_scale = int(entry["output_scale"])
    operands = entry.get("operands", [])
    values = [
        FixedValue(
            units=int(operand["units"]),
            scale=int(operand["scale"]),
            low=Fraction(0),
            high=Fraction(0),
        )
        for operand in operands
    ]
    if operation == "ADD" and len(values) == 2:
        value = values[0].real + values[1].real
    elif operation == "MUL" and len(values) == 2:
        value = values[0].real * values[1].real
    elif operation == "MIN" and len(values) == 2:
        value = min(values[0].real, values[1].real)
    elif operation == "DIV" and len(values) == 2:
        value = (
            values[0].real / values[1].real if values[1].real != 0 else Fraction(0)
        )
    elif operation == "RESCALE" and len(values) == 1:
        value = values[0].real
    else:
        raise ValueError(f"unsupported or malformed operation: {operation}")
    units, remainder = quantize_units(value, output_scale)
    return units, output_scale, remainder
