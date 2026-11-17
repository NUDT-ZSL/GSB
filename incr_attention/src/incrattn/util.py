"""Deterministic hashing and small shared helpers."""

from __future__ import annotations

import hashlib
import json

import numpy as np


def digest_bytes(parts):
    """Feed a sequence of byte-like / text parts into one SHA-256 digest."""
    hasher = hashlib.sha256()
    for part in parts:
        if isinstance(part, str):
            part = part.encode("utf-8")
        hasher.update(part)
        hasher.update(b"|")
    return hasher.hexdigest()


def array_fingerprint(array: np.ndarray) -> str:
    """Fingerprint raw array bytes, including dtype and shape."""
    array = np.ascontiguousarray(array)
    return digest_bytes(
        [
            str(array.shape),
            str(array.dtype),
            array.tobytes(order="C"),
        ]
    )


def canonical_json(data) -> str:
    """Stable JSON text used inside fingerprints and reports."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


def block_span(total: int, block_size: int, block_index: int) -> tuple[int, int]:
    """Half-open row span [start, end) with an exact, possibly short, tail."""
    start = block_index * block_size
    end = min(total, start + block_size)
    return start, end


def num_blocks(total: int, block_size: int) -> int:
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    return ceil_div(total, block_size)
