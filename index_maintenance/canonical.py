"""Canonical JSON, deterministic digests, and fixed-point helpers."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP
from typing import Any

import numpy as np

from .errors import BusinessError, E_BAD_OP


ROUNDING_MODES = {
    "HALF_UP": ROUND_HALF_UP,
    "HALF_EVEN": ROUND_HALF_EVEN,
    "UP": ROUND_UP,
    "DOWN": ROUND_DOWN,
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def json_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    # NumPy is part of the declared runtime and is used to materialize the
    # byte buffer before cryptographic hashing, keeping digest construction
    # identical across supported platforms.
    array = np.frombuffer(data, dtype=np.uint8)
    return hashlib.sha256(array.tobytes()).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(json_bytes(value))


def parse_decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("not a decimal")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        return Decimal(str(value))
    raise ValueError("not a decimal")


def fixed_decimal_string(value: Any, scale: int, rounding: str) -> str:
    try:
        decimal_value = parse_decimal(value)
    except (ValueError, ArithmeticError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc
    quantum = Decimal(1).scaleb(-scale)
    mode = ROUNDING_MODES[rounding]
    quantized = decimal_value.quantize(quantum, rounding=mode)
    if quantized == 0:
        quantized = abs(quantized)
    return format(quantized, f".{scale}f")


def fixed_units(value: Any, scale: int, rounding: str) -> int:
    text = fixed_decimal_string(value, scale, rounding)
    return int(Decimal(text).scaleb(scale))


def normalize_value(
    value: Any,
    column_type: str,
    *,
    scale: int,
    rounding: str,
    field: str,
) -> Any:
    if value is None:
        return None
    if column_type == "INT":
        if isinstance(value, bool) or not isinstance(value, int):
            raise BusinessError(
                E_BAD_OP,
                "integer column received an incompatible value",
                {"field": field, "expected": "INT", "actual": type(value).__name__},
            )
        return value
    if column_type == "BOOL":
        if not isinstance(value, bool):
            raise BusinessError(
                E_BAD_OP,
                "boolean column received an incompatible value",
                {"field": field, "expected": "BOOL", "actual": type(value).__name__},
            )
        return value
    if column_type == "TEXT":
        if not isinstance(value, str):
            raise BusinessError(
                E_BAD_OP,
                "text column received an incompatible value",
                {"field": field, "expected": "TEXT", "actual": type(value).__name__},
            )
        return value
    if column_type == "DECIMAL":
        try:
            return fixed_decimal_string(value, scale, rounding)
        except (ValueError, ArithmeticError) as exc:
            raise BusinessError(
                E_BAD_OP,
                "decimal column received an incompatible value",
                {"field": field, "expected": "DECIMAL", "actual": str(value)},
            ) from exc
    raise BusinessError(
        E_BAD_OP,
        "unsupported column type",
        {"field": field, "type": column_type},
    )
