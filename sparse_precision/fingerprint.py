"""Canonical fingerprints for reproducible sample and result auditing."""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np


def _update_array(digest: Any, array: np.ndarray, dtype: str) -> None:
    canonical = np.ascontiguousarray(array, dtype=dtype)
    digest.update(canonical.tobytes(order="C"))


def csr_fingerprint(matrix: Any) -> str:
    digest = hashlib.sha256()
    digest.update(b"csr-v1")
    digest.update(np.asarray(matrix.shape, dtype="<i8").tobytes())
    _update_array(digest, np.asarray(matrix.indptr), "<i8")
    _update_array(digest, np.asarray(matrix.indices), "<i8")
    _update_array(digest, np.asarray(matrix.data), "<f8")
    return digest.hexdigest()


def dense_fingerprint(array: np.ndarray) -> str:
    digest = hashlib.sha256()
    digest.update(b"dense-v1")
    _update_array(digest, np.asarray(array.shape, dtype="<i8"), "<i8")
    _update_array(digest, array, "<f8")
    return digest.hexdigest()


def bytes_fingerprint(array: np.ndarray) -> str:
    digest = hashlib.sha256()
    digest.update(b"array-f64-v1")
    _update_array(digest, np.asarray(array.shape, dtype="<i8"), "<i8")
    _update_array(digest, array, "<f8")
    return digest.hexdigest()

