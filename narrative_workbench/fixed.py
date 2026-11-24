from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, getcontext
from typing import Any

from .errors import DomainError

getcontext().prec = 80


def decimal_from_json(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise DomainError(
            "E_FIXED", f"{field} must be a finite decimal number", {"field": field}
        )
    try:
        if isinstance(value, Decimal):
            result = value
        elif isinstance(value, int):
            result = Decimal(value)
        elif isinstance(value, float):
            result = Decimal(str(value))
        elif isinstance(value, str):
            result = Decimal(value)
        else:
            raise InvalidOperation
    except (InvalidOperation, ValueError) as exc:
        raise DomainError(
            "E_FIXED",
            f"{field} must be a finite decimal number",
            {"field": field, "value": repr(value)},
        ) from exc
    if not result.is_finite():
        raise DomainError(
            "E_FIXED", f"{field} must be finite", {"field": field, "value": str(value)}
        )
    return result


def quantize_decimal(value: Any, scale: int, *, field: str) -> int:
    number = decimal_from_json(value, field=field)
    try:
        scaled = number * scale
        integral = scaled.to_integral_value(rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, OverflowError) as exc:
        raise DomainError(
            "E_FIXED", f"{field} cannot be quantized", {"field": field}
        ) from exc
    return int(integral)


def fixed_to_decimal(micros: int, scale: int) -> Decimal:
    return Decimal(micros) / Decimal(scale)


def fixed_text(micros: int, scale: int) -> str:
    number = fixed_to_decimal(micros, scale)
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def fixed_to_float(micros: int, scale: int) -> float:
    return float(fixed_to_decimal(micros, scale))


def compare_fixed(left: int, op: str, right: int) -> bool:
    if op == "eq":
        return left == right
    if op == "neq":
        return left != right
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    raise DomainError("E_OP", f"unknown numeric comparison operator: {op}")


def compare_float(left: float, op: str, right: float) -> bool:
    if op == "eq":
        return left == right
    if op == "neq":
        return left != right
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    raise DomainError("E_OP", f"unknown numeric comparison operator: {op}")


def error_decimal(fixed_micros: int, reference_value: float, scale: int) -> Decimal:
    reference = Decimal.from_float(reference_value)
    return abs(reference - fixed_to_decimal(fixed_micros, scale))

