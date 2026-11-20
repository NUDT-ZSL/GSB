"""Deterministic hashing helpers used for content checksums and fingerprints.

Only the Python standard library (:mod:`hashlib`, :mod:`json`) is used so the
checksums are stable across machines, Python builds and worker processes.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

import numpy as np


def stable_json_bytes(obj: Any) -> bytes:
    """Canonical JSON encoding: sorted keys, no whitespace surprises."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def checksum_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checksum_text(text: str) -> str:
    return checksum_bytes(text.encode("utf-8"))


def checksum_json(obj: Any) -> str:
    return checksum_bytes(stable_json_bytes(obj))


def checksum_numpy(array: np.ndarray) -> str:
    """Checksum over dtype, shape and the raw little-endian byte buffer."""
    arr = np.ascontiguousarray(array)
    digest = hashlib.sha256()
    digest.update(stable_json_bytes({"shape": list(arr.shape), "dtype": str(arr.dtype)}))
    digest.update(arr.astype(arr.dtype, copy=False).tobytes(order="C"))
    return digest.hexdigest()


def checksum_numpy_many(arrays: Iterable[np.ndarray]) -> str:
    digest = hashlib.sha256()
    for array in arrays:
        digest.update(checksum_numpy(array).encode("ascii"))
    return digest.hexdigest()


def checksum_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
