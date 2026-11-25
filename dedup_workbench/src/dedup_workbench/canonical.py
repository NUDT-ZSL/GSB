"""Canonical JSON and SHA-256 helpers used by every cached artifact."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def hash_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def write_canonical_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{__import__('os').getpid()}")
    payload = canonical_bytes(value)
    temporary.write_bytes(payload)
    temporary.replace(path)
    return sha256_bytes(payload)
