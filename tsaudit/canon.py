"""Canonical JSON and digest helpers."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def sha256_obj(value: Any) -> str:
    return sha256_hex(canonical_bytes(value))


def strict_json_loads(text: str) -> Any:
    return json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(
        ValueError(f"invalid JSON constant: {value}")
    ))


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_issuer_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def stable_node_key(prefix: str, value: Any) -> str:
    return f"{prefix}:{canonical_text(value) if not isinstance(value, (str, int)) else value}"
