"""Shared deterministic serialization and path helpers."""

from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .errors import E_BAD_EVENT, E_BAD_LIMITER, LedgerError

TOKEN_SCALE = 1_000_000
LEVELS = ("tenant", "bucket", "prefix")
LEVEL_RANK = {"tenant": 0, "bucket": 1, "prefix": 2}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def pretty_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle, parse_float=Decimal)


def make_path(level: str, tenant: str, bucket: str = "", prefix: str = "") -> str:
    if level == "tenant":
        return f"tenant:{quote(tenant, safe='')}"
    if level == "bucket":
        return f"tenant:{quote(tenant, safe='')}/bucket:{quote(bucket, safe='')}"
    if level == "prefix":
        return (
            f"tenant:{quote(tenant, safe='')}/bucket:{quote(bucket, safe='')}"
            f"/prefix:{quote(prefix, safe='')}"
        )
    raise ValueError(f"unknown level: {level}")


def layer_sort_key(layer: Any) -> tuple[int, str, str, str]:
    return (
        LEVEL_RANK[layer.level],
        layer.tenant,
        layer.bucket,
        layer.prefix,
    )


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def require_text(value: Any, field: str, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(code, f"{field} must be a non-empty string")
    if len(value) > 512:
        raise LedgerError(code, f"{field} is too long", {"field": field})
    if any(ord(char) < 32 for char in value):
        raise LedgerError(code, f"{field} contains control characters")
    return value


def require_int(
    value: Any,
    field: str,
    code: str,
    *,
    minimum: int | None = None,
) -> int:
    if not is_int(value):
        raise LedgerError(code, f"{field} must be an integer", {"field": field})
    if minimum is not None and value < minimum:
        raise LedgerError(
            code,
            f"{field} must be >= {minimum}",
            {"field": field, "value": value},
        )
    return value


def require_bool(value: Any, field: str, code: str) -> bool:
    if not isinstance(value, bool):
        raise LedgerError(code, f"{field} must be a boolean", {"field": field})
    return value


def parse_positive_rate(value: Any, field: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise LedgerError(E_BAD_LIMITER, f"{field} must be a finite positive number")
    if isinstance(value, str):
        try:
            decimal_value = Decimal(value)
        except Exception as exc:
            raise LedgerError(
                E_BAD_LIMITER,
                f"{field} must be a finite positive number",
            ) from exc
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise LedgerError(E_BAD_LIMITER, f"{field} must be a finite positive number")
        decimal_value = Decimal(str(value))
    elif isinstance(value, Decimal):
        decimal_value = value
    else:
        decimal_value = Decimal(value)
    if not decimal_value.is_finite() or decimal_value <= 0:
        raise LedgerError(E_BAD_LIMITER, f"{field} must be a finite positive number")
    return Fraction(decimal_value)


def fraction_text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    # Rates originate from JSON decimals, so a finite decimal representation exists.
    result = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def tokens_text(tokens_micro: int) -> str:
    sign = "-" if tokens_micro < 0 else ""
    absolute = abs(tokens_micro)
    return f"{sign}{absolute // TOKEN_SCALE}.{absolute % TOKEN_SCALE:06d}"


def event_required_fields() -> tuple[str, ...]:
    return (
        "seq",
        "tenant",
        "bucket",
        "prefix",
        "object_key",
        "bytes",
        "deleted",
        "clock_ms",
        "idempotency_key",
    )


def event_error(message: str, **details: Any) -> LedgerError:
    return LedgerError(E_BAD_EVENT, message, details)

