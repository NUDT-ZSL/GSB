"""Deterministic canonical serialization and content checksums.

All snapshot replacement and byte-for-byte reuse checks rely on a single
canonical JSON encoding: keys are emitted in sorted order, NumPy scalars are
converted to plain Python numbers and no whitespace is used.  The resulting
string is fully reproducible across runs and platforms for the same object.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np


def _default(value: Any) -> Any:
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "value") and value.__class__.__name__ in {"RoundingMode"}:
        return value.value
    raise TypeError(f"object of type {type(value).__name__} is not canonical serializable")


def canonical_dumps(obj: Any) -> str:
    """Return the deterministic compact JSON representation of ``obj``."""

    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_default,
        allow_nan=False,
    )


def canonical_bytes(obj: Any) -> bytes:
    return canonical_dumps(obj).encode("utf-8")


def content_checksum(obj: Any) -> str:
    """SHA-256 checksum of the canonical encoding."""

    return hashlib.sha256(canonical_bytes(obj)).hexdigest()
