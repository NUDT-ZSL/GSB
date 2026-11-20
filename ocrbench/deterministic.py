"""Determinism primitives shared across the whole pipeline.

All hashing uses canonical JSON encoding with sorted keys and a fixed
separator so that identical logical content always serializes to identical
bytes, independent of dict insertion order.  No wall-clock time, process id,
random OS state or hash randomization is ever used for cache identity.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

import numpy as np


def canonical_bytes(obj: Any) -> bytes:
    """Serialize ``obj`` to deterministic canonical JSON bytes.

    ``sort_keys=True`` removes any dependence on dictionary traversal order
    and explicit separators avoid platform dependent whitespace.
    """

    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_hash(obj: Any) -> str:
    """Return the SHA-256 hex digest of the canonical encoding of ``obj``."""

    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def hash_bytes(data: bytes) -> str:
    """Return the SHA-256 hex digest of raw bytes."""

    return hashlib.sha256(data).hexdigest()


def seeded_rng(*parts: Any) -> np.random.Generator:
    """Build a deterministic NumPy ``Generator`` from stable seed parts.

    Each part is canonicalized and folded into a 64 bit integer seed via
    SHA-256, so structured inputs (strings, tuples, ints) produce the same
    generator on every run and platform.
    """

    digest = hashlib.sha256(canonical_bytes(list(parts))).digest()
    seed = int.from_bytes(digest[:8], "big", signed=False)
    return np.random.default_rng(seed)


def quantize(value: float, scale: int = 1000) -> int:
    """Quantize a float to a fixed point integer for byte-identical output.

    Floating point features are rounded half-to-even onto a fixed grid so the
    recognition strings, confidence values and metrics serialize identically
    regardless of tiny BLAS/ordering differences.
    """

    return int(np.round(float(value) * scale))


def to_jsonable(obj: Any) -> Any:
    """Convert NumPy/scalar containers into plain JSON-serialisable data."""

    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


def stable_summary(values: Iterable[Any]) -> str:
    """Return a short stable hash summarizing an ordered collection."""

    return content_hash(list(values))[:16]
