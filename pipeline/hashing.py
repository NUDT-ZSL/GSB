"""Deterministic canonical JSON serialization and content hashing."""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Separators without whitespace make the canonical form compact and stable
#: across platforms; sort_keys removes any dependence on insertion order.
_CANON_KW = {"sort_keys": True, "separators": (",", ":"), "ensure_ascii": False}


def canonical_json(obj: Any) -> str:
    """Serialize ``obj`` to a single canonical JSON string."""
    return json.dumps(obj, **_CANON_KW)


def canonical_bytes(obj: Any) -> bytes:
    """Canonical JSON encoded as UTF-8."""
    return canonical_json(obj).encode("utf-8")


def content_hash(obj: Any) -> str:
    """SHA-256 hex digest of the canonical JSON form of ``obj``."""
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def file_hash(path: str) -> str:
    """SHA-256 hex digest of a file's raw bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()
