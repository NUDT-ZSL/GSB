"""Canonical hashing helpers shared by artifacts, plans, and the ledger."""

from __future__ import annotations

import hashlib
import io
import json
import math
from typing import Any, Mapping

import numpy as np


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def array_sha256(array: np.ndarray) -> str:
    """Hash an array's dtype, shape, and C-order bytes, regardless of layout."""

    contiguous = np.ascontiguousarray(array)
    digest = hashlib.sha256()
    digest.update(contiguous.dtype.str.encode("ascii"))
    digest.update(b"|")
    digest.update(",".join(str(size) for size in contiguous.shape).encode("ascii"))
    digest.update(b"|")
    digest.update(contiguous.tobytes(order="C"))
    return digest.hexdigest()


def npy_bytes(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, np.asarray(array), allow_pickle=False)
    return buffer.getvalue()


def finite_float(value: float | np.floating[Any]) -> float | None:
    value_float = float(value)
    return value_float if math.isfinite(value_float) else None


def digest_dependencies(dependencies: Mapping[str, Any] | list[Any]) -> str:
    return sha256_json(dependencies)

