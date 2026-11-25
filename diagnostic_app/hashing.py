from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np


def canonical_json(value: Any) -> bytes:
    """Serialize JSON data with one canonical byte representation."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def array_digest(array: np.ndarray) -> str:
    """Hash dtype/shape and raw little-endian payload.

    The payload is explicitly converted to C-contiguous bytes so transposed
    views do not accidentally hash with non-contiguous memory layout.
    """
    contiguous = np.ascontiguousarray(array)
    hasher = hashlib.sha256()
    hasher.update(str(contiguous.dtype).encode("ascii"))
    hasher.update(b"|")
    hasher.update(json.dumps(list(contiguous.shape)).encode("ascii"))
    hasher.update(b"|")
    hasher.update(contiguous.tobytes(order="C"))
    return hasher.hexdigest()


def pixel_digest(array: np.ndarray) -> str:
    """Compatibility alias used by product hash records."""
    return array_digest(array)
