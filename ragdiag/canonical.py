"""Canonical serialization, hashing, and float64 normalization."""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP
from typing import Any, Iterable, Sequence

import numpy as np


ROUNDING_MODES = {
    "half_even": ROUND_HALF_EVEN,
    "half_up": ROUND_HALF_UP,
    "toward_zero": ROUND_DOWN,
    "up": ROUND_UP,
}


def canonical_bytes(value: Any) -> bytes:
    """Serialize a JSON-compatible value in a byte-stable representation."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_value(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def content_hash(content: Any) -> str:
    if isinstance(content, str):
        payload = content.encode("utf-8")
    elif isinstance(content, bytes):
        payload = content
    else:
        payload = canonical_bytes(content)
    return sha256_bytes(payload)


def checksum_of(content: Any) -> str:
    return f"sha256:{content_hash(content)}"


def select_rounding(mode: str):
    try:
        return ROUNDING_MODES[mode]
    except KeyError as exc:
        raise ValueError(f"unknown rounding mode: {mode}") from exc


def quantize_float(
    value: float,
    decimals: int,
    rounding_mode: str = "half_even",
    protect_subnormal: bool = True,
) -> float:
    """Quantize a finite float using a declared decimal policy."""

    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite float: {number}")
    if protect_subnormal and 0.0 < abs(number) < np.finfo(np.float64).tiny:
        number = 0.0
    quantum = Decimal(1).scaleb(-decimals)
    rounded = Decimal(str(number)).quantize(
        quantum, rounding=select_rounding(rounding_mode)
    )
    return float(rounded)


def stable_float_array(
    values: Sequence[float] | Iterable[float],
    precision: dict[str, Any],
) -> np.ndarray:
    """Return a C-contiguous float64 array after quantization."""

    decimals = int(precision.get("score_decimals", 10))
    rounding_mode = str(precision.get("rounding_mode", "half_even"))
    protect = bool(precision.get("subnormal_protection", True))
    normalized = [
        quantize_float(v, decimals, rounding_mode, protect) for v in values
    ]
    return np.asarray(normalized, dtype=np.float64, order="C")


def float_array_bytes(values: Sequence[float], precision: dict[str, Any]) -> bytes:
    return stable_float_array(values, precision).tobytes(order="C")


def first_array_deviation(left: np.ndarray, right: np.ndarray) -> dict[str, Any] | None:
    """Locate the first byte and element deviation between two arrays."""

    left_bytes = np.ascontiguousarray(left, dtype=np.float64).tobytes(order="C")
    right_bytes = np.ascontiguousarray(right, dtype=np.float64).tobytes(order="C")
    if left_bytes == right_bytes:
        return None
    byte_limit = min(len(left_bytes), len(right_bytes))
    byte_index = next(
        (i for i in range(byte_limit) if left_bytes[i] != right_bytes[i]),
        byte_limit,
    )
    element_index = byte_index // 8

    def at(array: np.ndarray, index: int) -> float | None:
        flat = np.ascontiguousarray(array, dtype=np.float64).reshape(-1)
        return float(flat[index]) if index < flat.size else None

    return {
        "byte_index": byte_index,
        "element_index": element_index,
        "left": at(left, element_index),
        "right": at(right, element_index),
    }


def format_float(value: float, precision: dict[str, Any]) -> float:
    return quantize_float(
        value,
        int(precision.get("score_decimals", 10)),
        str(precision.get("rounding_mode", "half_even")),
        bool(precision.get("subnormal_protection", True)),
    )

