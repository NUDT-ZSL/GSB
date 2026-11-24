"""Canonical encoding, hashing, and strict JSON loading."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class DuplicateJsonKey(ValueError):
    pass


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def strict_json_loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_no_duplicate_object)


def load_json(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return strict_json_loads(handle.read())


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_obj(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def make_sync_marker(paths: dict[str, str | Path]) -> dict[str, Any]:
    files = {name: sha256_file(path) for name, path in sorted(paths.items())}
    marker = sha256_obj({"files": files})
    return {
        "format": "certpath.sync",
        "version": 1,
        "sync_marker": marker,
        "files": files,
    }
