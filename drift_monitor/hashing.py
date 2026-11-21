"""Deterministic canonical JSON and content hashing helpers.

The whole pipeline relies on byte-for-byte reproducible artefacts.  We
therefore never hash ``str(dict)`` (which depends on insertion order and
platform float formatting).  Instead every structure is reduced to a canonical
JSON document with sorted keys and fixed float formatting, then hashed with
SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def canonicalize(obj: Any) -> str:
    """Return a deterministic JSON string for ``obj``.

    * Mapping keys are sorted.
    * Floats use ``repr`` so that the same double always renders identically;
      NaN/+-Infinity are replaced by explicit sentinel strings because plain
      JSON has no representation for them.
    """

    def default(value: Any) -> Any:
        if isinstance(value, bytes):
            return {"__bytes__": value.hex()}
        if isinstance(value, set):
            return {"__set__": sorted(canonicalize(v) for v in value)}
        raise TypeError(f"cannot canonicalize type {type(value)!r}")

    return json.dumps(
        _normalize(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=default,
        allow_nan=False,
    )


def _normalize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _normalize(obj[k]) for k in sorted(obj, key=str)}
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj):
            return {"__float__": "nan"}
        if math.isinf(obj):
            return {"__float__": "inf" if obj > 0 else "-inf"}
        return obj
    if isinstance(obj, (int, bool, str)) or obj is None:
        return obj
    if isinstance(obj, bytes):
        return {"__bytes__": obj.hex()}
    if isinstance(obj, set):
        return {"__set__": sorted(canonicalize(_normalize(v)) for v in obj)}
    # NumPy scalars / arrays are handled via their python equivalents.
    if hasattr(obj, "tolist"):
        return _normalize(obj.tolist())
    if hasattr(obj, "item"):
        return _normalize(obj.item())
    raise TypeError(f"cannot canonicalize type {type(obj)!r}")


def content_hash(obj: Any) -> str:
    """SHA-256 hex digest of the canonical encoding of ``obj``."""
    return hashlib.sha256(canonicalize(obj).encode("utf-8")).hexdigest()


def short_hash(obj: Any, length: int = 16) -> str:
    return content_hash(obj)[:length]
