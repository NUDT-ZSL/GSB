"""Deterministic JSON, digest, and fixed-point helpers."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import numpy as np


def normalize_floats(value: Any) -> Any:
    """Convert NumPy scalars and normalize negative zero recursively."""
    if isinstance(value, dict):
        return {str(k): normalize_floats(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_floats(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite value cannot be canonically encoded")
        if value == 0.0:
            return 0.0
        return float(value)
    return value


def canonical_json(value: Any) -> bytes:
    value = normalize_floats(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_digest(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quantize_fixed(value: float, scale: int) -> int:
    """Quantize with deterministic half-away-from-zero rounding."""
    if not isinstance(scale, int) or isinstance(scale, bool) or scale <= 0:
        raise ValueError("scale must be a positive integer")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("cannot quantize a non-finite value")
    scaled = abs(value) * scale
    quantum = math.floor(scaled + 0.5)
    return int(quantum if value >= 0 else -quantum)


def fixed_to_float(quantum: int, scale: int) -> float:
    return int(quantum) / int(scale)


def sqlite_safe_integer(value: int) -> int:
    value = int(value)
    if value > 2**63 - 1 or value < -(2**63):
        raise OverflowError("fixed-point quantum exceeds SQLite signed INTEGER range")
    return value
