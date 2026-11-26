"""Canonical serialization and content hashing helpers."""
from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np


def canonical_json(obj: Any) -> str:
    """Return a stable JSON string with sorted keys and no whitespace."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (set, tuple)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


def hash_json(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def derive_seed(*parts: Any) -> int:
    """Derive a deterministic uint64 seed from arbitrary canonical parts."""
    digest = hashlib.sha256(canonical_json(list(parts)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def array_digest(arr: np.ndarray) -> str:
    """Hash shape/dtype together with raw bytes of an ndarray."""
    hasher = hashlib.sha256()
    hasher.update(str(arr.shape).encode())
    hasher.update(b"|")
    hasher.update(str(arr.dtype).encode())
    hasher.update(b"|")
    hasher.update(np.ascontiguousarray(arr).tobytes())
    return hasher.hexdigest()
