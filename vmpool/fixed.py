"""Controlled fixed-point arithmetic and canonical serialization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, localcontext
from typing import Any

from .errors import E_INVALID_NUMBER, BusinessError


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def digest_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def decimal_value(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise BusinessError(
            E_INVALID_NUMBER,
            "布尔值不能作为数值",
            {"field": field, "value": value},
        )
    try:
        if isinstance(value, Decimal):
            result = value
        elif isinstance(value, int):
            result = Decimal(value)
        elif isinstance(value, str):
            result = Decimal(value)
        elif isinstance(value, float):
            result = Decimal(repr(value))
        else:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise BusinessError(
            E_INVALID_NUMBER,
            "不是合法十进制数",
            {"field": field, "value": repr(value)},
        ) from None
    if not result.is_finite():
        raise BusinessError(
            E_INVALID_NUMBER,
            "数值必须有限",
            {"field": field, "value": str(value)},
        )
    return result


def positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BusinessError(
            E_INVALID_NUMBER,
            "必须是正整数",
            {"field": field, "value": repr(value)},
        )
    return value


def nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BusinessError(
            E_INVALID_NUMBER,
            "必须是非负整数",
            {"field": field, "value": repr(value)},
        )
    return value


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        return "0"
    return text


def jsonable(value: Any) -> Any:
    """Convert parsed Decimal values into lossless strings for storage."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    return value


@dataclass(frozen=True)
class Conversion:
    conversion_id: str
    field_path: str
    source_value: str
    source_float_repr: str
    source_float_value: float | None
    scale: int
    rounding: str
    fixed_value: int
    error_upper_bound_fixed: int
    error_upper_bound: str
    operation: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "conversion_id": self.conversion_id,
            "field_path": self.field_path,
            "source_value": self.source_value,
            "source_float_repr": self.source_float_repr,
            "source_float_value": self.source_float_value,
            "scale": self.scale,
            "rounding": self.rounding,
            "fixed_value": self.fixed_value,
            "error_upper_bound_fixed": self.error_upper_bound_fixed,
            "error_upper_bound": self.error_upper_bound,
            "operation": self.operation,
        }


def _rounding_mode(name: str):
    if name == "floor":
        return ROUND_FLOOR
    raise BusinessError(
        E_INVALID_NUMBER,
        "不支持的舍入方向",
        {"rounding": name},
    )


def convert_to_fixed(
    value: Any,
    scale: int,
    field_path: str,
    conversion_id: str,
    *,
    rounding: str = "floor",
    operation: str = "decimal*scale",
) -> tuple[int, Conversion]:
    if isinstance(scale, bool) or not isinstance(scale, int) or scale <= 0:
        raise BusinessError(
            E_INVALID_NUMBER,
            "定点标度必须是正整数",
            {"field": field_path, "scale": repr(scale)},
        )
    source = decimal_value(value, field_path)
    mode = _rounding_mode(rounding)
    with localcontext() as ctx:
        ctx.prec = max(50, len(source.as_tuple().digits) + 30)
        exact = source * Decimal(scale)
        fixed = int(exact.to_integral_value(rounding=mode))
    residual = abs(exact - Decimal(fixed))
    bound_fixed = int(residual.to_integral_value(rounding=ROUND_CEILING))
    try:
        source_float = float(source)
        float_repr = repr(source_float)
        if source_float in (float("inf"), float("-inf")):
            source_float = None
    except (OverflowError, ValueError):
        float_repr = "non-float"
        source_float = None
    conversion = Conversion(
        conversion_id=conversion_id,
        field_path=field_path,
        source_value=format(source, "f"),
        source_float_repr=float_repr,
        source_float_value=source_float,
        scale=scale,
        rounding=rounding,
        fixed_value=fixed,
        error_upper_bound_fixed=bound_fixed,
        error_upper_bound=decimal_text(residual),
        operation=operation,
    )
    return fixed, conversion


def ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return -((-numerator) // denominator)


def apply_safety_margin(amount: int, ratio_fixed: int, ratio_denominator: int) -> int:
    if amount < 0:
        return -apply_safety_margin(-amount, ratio_fixed, ratio_denominator)
    return ceil_div(
        amount * (ratio_denominator + ratio_fixed),
        ratio_denominator,
    )


def fixed_with_unit(value: int, scale: int) -> str:
    """Render a fixed integer without binary floating point."""
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    whole, fraction = divmod(absolute, scale)
    if fraction == 0:
        return f"{sign}{whole}"
    digits = str(scale)
    width = len(digits) - 1
    return f"{sign}{whole}.{str(fraction).rjust(width, '0').rstrip('0')}"
