"""Deterministic content digests and canonical JSON serialization.

Version identifiers are SHA-256 hashes over canonical (key-sorted,
whitespace-free) JSON so that equivalent inputs always hash identically,
independent of dict insertion order or platform.  Numeric arrays are hashed
over their raw little-endian bytes together with explicit shape/dtype
metadata, which makes a corrupted or truncated tile detectable.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

import numpy as np


def canonical_json(obj: Any) -> str:
    """Serialize ``obj`` deterministically.

    Keys are sorted, separators carry no decorative whitespace and numpy
    scalars are converted to plain Python types first.
    """

    def default(value: Any) -> Any:
        if isinstance(value, np.ndarray):
            return {"__ndarray__": True, "shape": list(value.shape),
                    "dtype": str(value.dtype), "data": value.tolist()}
        if isinstance(value, (np.integer,)):
            return int(value)
        if isinstance(value, (np.floating,)):
            return float(value)
        raise TypeError(f"cannot canonicalize {type(value)!r}")

    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False, default=default,
    )


def digest_canonical(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def digest_bytes(chunks: Iterable[bytes]) -> str:
    hasher = hashlib.sha256()
    for chunk in chunks:
        hasher.update(chunk)
    return hasher.hexdigest()


def digest_heights(heights: np.ndarray) -> str:
    """Hash an integer height field over shape + raw bytes."""
    array = np.ascontiguousarray(heights, dtype=np.int64)
    header = canonical_json(
        {"kind": "heightfield", "shape": list(array.shape),
         "dtype": "int64"}).encode("utf-8")
    return digest_bytes([header, b"\0", array.tobytes()])


def digest_tile_arrays(gray: np.ndarray, depth: np.ndarray) -> str:
    """Hash the two pixel arrays of a rendered tile."""
    gray = np.ascontiguousarray(gray, dtype=np.uint8)
    depth = np.ascontiguousarray(depth, dtype=np.float32)
    header = canonical_json({
        "kind": "tile-arrays",
        "gray_shape": list(gray.shape),
        "depth_shape": list(depth.shape),
        "gray_dtype": "uint8",
        "depth_dtype": "float32",
    }).encode("utf-8")
    return digest_bytes([header, b"\0", gray.tobytes(), depth.tobytes()])

