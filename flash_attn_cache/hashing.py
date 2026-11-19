from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

import numpy as np


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_object(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def canonical_array(array: np.ndarray) -> np.ndarray:
    array = np.asarray(array)
    if array.dtype.kind != "f" or array.dtype.itemsize not in (4, 8):
        raise TypeError(f"unsupported cache array dtype: {array.dtype}")
    return np.ascontiguousarray(array)


def array_descriptors(
    arrays: Mapping[str, np.ndarray],
    metadata: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], bytes]:
    descriptors: list[dict[str, Any]] = []
    chunks: list[bytes] = []
    for name in sorted(arrays):
        array = canonical_array(arrays[name])
        data = array.tobytes(order="C")
        descriptors.append(
            {
                "name": name,
                "dtype": array.dtype.str,
                "shape": list(array.shape),
                "nbytes": len(data),
            }
        )
        chunks.append(data)
    fingerprint = {
        "schema": "fac-array-content-v1",
        "metadata": dict(metadata or {}),
        "arrays": descriptors,
    }
    prefix = canonical_json(fingerprint)
    return descriptors, prefix + b"\x00" + b"".join(chunks)


def array_content_hash(
    arrays: Mapping[str, np.ndarray],
    metadata: Mapping[str, Any] | None = None,
) -> str:
    _, payload = array_descriptors(arrays, metadata)
    return sha256_bytes(payload)


def chunk_hash(array: np.ndarray, *, start: int, length: int, role: str) -> str:
    canonical = canonical_array(array)
    return array_content_hash(
        {role: canonical},
        {"role": role, "start": int(start), "length": int(length)},
    )
