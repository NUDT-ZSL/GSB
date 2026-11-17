"""Deterministic content addressing helpers (SHA-256 only)."""
from __future__ import annotations

import hashlib
import os

DIGEST_ALGO = "sha256"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    engine = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            engine.update(chunk)
    return engine.hexdigest()


def content_object_id(digest_hex: str) -> str:
    return f"{DIGEST_ALGO}-{digest_hex}"


def object_relpath(object_id: str) -> str:
    algo, digest_hex = object_id.split("-", 1)
    return os.path.join("objects", algo, digest_hex[:2], digest_hex)

