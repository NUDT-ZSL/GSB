from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, (np.ndarray, np.generic)):
        return value.tolist()
    if isinstance(value, (set, tuple)):
        return list(value)
    raise TypeError(f"cannot canonicalize {type(value)!r}")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_text(canonical_json(value))


def short_hash(value: Any, length: int = 16) -> str:
    return canonical_hash(value)[:length]


def array_digest(array: np.ndarray | None) -> str:
    if array is None:
        return "none"
    contiguous = np.ascontiguousarray(array)
    return hashlib.sha256(contiguous.tobytes()).hexdigest()


def arrays_digest(arrays: dict[str, np.ndarray]) -> str:
    hasher = hashlib.sha256()
    for name in sorted(arrays):
        array = np.ascontiguousarray(arrays[name])
        hasher.update(name.encode("utf-8"))
        hasher.update(str(array.dtype).encode("ascii"))
        hasher.update(str(array.shape).encode("ascii"))
        hasher.update(array.tobytes())
    return hasher.hexdigest()


def content_hash(payload: dict[str, Any], arrays: dict[str, np.ndarray] | None = None) -> str:
    hasher = hashlib.sha256()
    hasher.update(canonical_json(payload).encode("utf-8"))
    if arrays:
        hasher.update(arrays_digest(arrays).encode("ascii"))
    return hasher.hexdigest()
