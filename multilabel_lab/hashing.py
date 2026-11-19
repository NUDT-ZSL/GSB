"""Canonical serialization and byte-level hashing helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON deterministically, rejecting NaN and infinity."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def array_bytes(array: np.ndarray, dtype: str = "<f8") -> bytes:
    """Return a canonical header followed by C-order little-endian bytes."""

    contiguous = np.ascontiguousarray(array, dtype=np.dtype(dtype))
    header = canonical_json_bytes(
        {"dtype": contiguous.dtype.str, "shape": list(contiguous.shape)}
    )
    return header + b"\x00" + contiguous.tobytes(order="C")


def array_digest(array: np.ndarray, dtype: str = "<f8") -> str:
    return sha256_bytes(array_bytes(array, dtype=dtype))
