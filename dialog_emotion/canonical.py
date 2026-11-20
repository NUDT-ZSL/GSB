"""Canonical JSON, hashing, and deterministic numeric helpers."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import numpy as np


def _normalize(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return [_normalize(item) for item in value.tolist()]
    if isinstance(value, np.generic):
        return _normalize(value.item())
    if isinstance(value, dict):
        return {str(key): _normalize(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON does not allow NaN or infinity")
        if value == 0.0:
            return 0.0
    return value


def canonical_bytes(value: Any) -> bytes:
    normalized = _normalize(value)
    text = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return text.encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def stable_seed(*parts: Any) -> int:
    digest = hashlib.blake2b(
        canonical_bytes(list(parts)), digest_size=16, person=b"dialog-emotion"
    ).digest()
    return int.from_bytes(digest, "little", signed=False)


def quantize(value: float, digits: int = 12) -> float:
    result = round(float(value), digits)
    if result == 0.0:
        return 0.0
    return result
