"""Small deterministic serialization and hashing helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np


def canonical_json_bytes(value: Any) -> bytes:
    """Stable UTF-8 JSON with sorted keys and no insignificant whitespace."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_text(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
    return canonical_json_bytes(value).decode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def array_digest(array: np.ndarray) -> str:
    """Hash dtype, shape, and C-order IEEE bytes (including signed zero)."""

    contiguous = np.ascontiguousarray(array)
    header = canonical_json_bytes(
        {
            "dtype": contiguous.dtype.str,
            "shape": list(contiguous.shape),
        }
    )
    digest = hashlib.sha256()
    digest.update(header)
    digest.update(b"\x00")
    digest.update(contiguous.tobytes(order="C"))
    return digest.hexdigest()


def json_float(value: float | np.floating[Any]) -> float:
    """Convert NumPy scalar to a finite JSON float."""

    result = float(value)
    if not np.isfinite(result):
        raise ValueError(f"non-finite float cannot be serialized: {result!r}")
    return result
