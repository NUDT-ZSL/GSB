"""Deterministic canonical hashing helpers."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

import numpy as np


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_hash(payload: Mapping[str, Any], *, namespace: str = "v1") -> str:
    material = f"{namespace}\n{canonical_json(payload)}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def array_content_hash(array: np.ndarray) -> str:
    """Hash dtype, shape and C-contiguous pixels, not object identity."""
    contiguous = np.ascontiguousarray(array)
    header = f"dtype={contiguous.dtype.str};shape={contiguous.shape}".encode("ascii")
    digest = hashlib.sha256()
    digest.update(header)
    digest.update(b";data=")
    digest.update(contiguous.tobytes(order="C"))
    return digest.hexdigest()


def aggregate_hashes(hashes: Mapping[int, str] | list[str]) -> str:
    if isinstance(hashes, list):
        items = list(enumerate(hashes))
    else:
        items = sorted(hashes.items())
    material = "\n".join(f"{index}:{value}" for index, value in items)
    return sha256_hex(material.encode("utf-8"))
