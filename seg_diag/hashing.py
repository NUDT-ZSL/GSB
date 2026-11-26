"""Canonical serialization and SHA-256 content addressing helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np


def to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.ndarray):
        return {
            "__ndarray__": True,
            "shape": list(value.shape),
            "dtype": str(value.dtype),
            "sha256": array_digest(value),
        }
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, bytes):
        return value.hex()
    return value


def canonical_dumps(value: Any) -> str:
    return json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value).encode("utf-8")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def content_key(value: Any) -> str:
    return sha256_hex(canonical_bytes(value))


def short_key(value: Any, length: int = 16) -> str:
    return content_key(value)[:length]


def array_digest(array: np.ndarray) -> str:
    arr = np.ascontiguousarray(array)
    return sha256_hex(arr.view(np.uint8).tobytes())


def file_digest(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
