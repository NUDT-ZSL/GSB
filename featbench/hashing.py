"""Deterministic hashing helpers: canonical JSON fingerprints and content digests."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

import numpy as np


def canonical_json(obj: Any) -> str:
    """Stable JSON text: sorted keys, fixed separators, no whitespace surprises."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=_json_default)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    raise TypeError(f"not canonicalizable: {type(obj)!r}")


def fingerprint(obj: Any, prefix: str = "") -> str:
    """Short stable fingerprint (blake2b, 16 bytes -> 32 hex chars)."""
    text = prefix + canonical_json(obj)
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


def hash_text(text: str, digest_size: int = 8) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=digest_size).hexdigest()


def hash_uint64(text: str) -> int:
    """Deterministic 64 bit unsigned hash used for cross value encoding."""
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "little")


def digest_array(arr: np.ndarray) -> str:
    """Content digest of a numeric array, shape and dtype included."""
    h = hashlib.blake2b(digest_size=16)
    h.update(str(arr.shape).encode())
    h.update(str(arr.dtype).encode())
    h.update(np.ascontiguousarray(arr).tobytes())
    return h.hexdigest()


def digest_iterable(values: Iterable[Any]) -> str:
    h = hashlib.blake2b(digest_size=16)
    for value in values:
        h.update(canonical_json(value).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()
