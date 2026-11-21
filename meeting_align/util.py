"""Canonical JSON + hashing helpers shared by all modules."""

from __future__ import annotations

import hashlib
import json

import numpy as np


def canonical_json(obj) -> str:
    """Deterministic JSON serialization (byte-identical for equal inputs)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def hash_obj(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_pcm(pcm: np.ndarray) -> str:
    arr = np.ascontiguousarray(pcm, dtype=np.int16)
    return hashlib.sha256(arr.tobytes()).hexdigest()


def checksum_obj(obj) -> str:
    """Short checksum used by versioned snapshots."""
    return hash_obj(obj)[:32]


def round_ms(value: float) -> float:
    """Quantize millisecond values so reruns are byte-identical."""
    return round(float(value), 6)
