from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    """Return the stable byte representation used by caches and ledgers."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_key(value: Any) -> str:
    """Content-addressable key for JSON-compatible configuration or results."""
    if isinstance(value, (bytes, bytearray)):
        return sha256_bytes(bytes(value))
    return sha256_bytes(canonical_bytes(value))


def short_key(key: str, length: int = 12) -> str:
    return key[:length]


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def atomic_write_json(path: Path, value: Any) -> str:
    data = canonical_bytes(value) + b"\n"
    atomic_write_bytes(path, data)
    return sha256_bytes(data)
