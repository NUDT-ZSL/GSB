"""Deterministic serialization and hashing helpers.

All cache keys, checksums and snapshot digests in the workbench are built
on canonical JSON (sorted keys, tight separators, NaN forbidden) hashed
with SHA-256.  Python's ``json`` module serializes floats with the
shortest round-trip repr, which is deterministic for identical float
values, so identical computations yield byte-identical artifacts.
"""

from __future__ import annotations

import hashlib
import json


def canonical_json(obj) -> str:
    """Serialize ``obj`` deterministically (sorted keys, no whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(obj) -> str:
    """Content-address of a JSON-serializable object."""
    return sha256_hex(canonical_json(obj))


def stable_seed(*parts) -> int:
    """Derive a deterministic 32-bit seed from arbitrary parts.

    Uses SHA-256 rather than the builtin ``hash`` so results are stable
    across processes and PYTHONHASHSEED settings.
    """
    material = "|".join(str(p) for p in parts)
    return int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:4], "big")


def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def floats_to_lists(obj):
    """Recursively convert numpy scalars/arrays to plain Python types."""
    import numpy as np

    if isinstance(obj, dict):
        return {k: floats_to_lists(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [floats_to_lists(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return floats_to_lists(obj.tolist())
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    return obj
