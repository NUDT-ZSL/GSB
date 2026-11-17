from __future__ import annotations

import hashlib
import json
import math
import struct
from typing import Any


def canonical_json(value: Any) -> str:
    """Canonical JSON used for hashing, persistence, and diagnostics."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def normalize_float(value: float) -> float:
    """Normalize only negative zero; all other binary64 values are preserved."""

    if not math.isfinite(value):
        raise ValueError("non-finite float")
    if value == 0.0:
        return 0.0
    return float(value)


def float_bits_hex(value: float) -> str:
    """Big-endian IEEE-754 binary64 bits, lowercase hex."""

    return struct.pack(">d", normalize_float(float(value))).hex()


def canonical_float_vector(values: list[float] | tuple[float, ...]) -> str:
    return "[" + ",".join(float_bits_hex(float(value)) for value in values) + "]"


def sorted_string_list(values: Any) -> list[str]:
    return sorted(str(value) for value in values)
