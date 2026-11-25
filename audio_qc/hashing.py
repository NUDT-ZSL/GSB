"""Canonical JSON and content hashing helpers.

The canonical form always sorts mapping keys explicitly.  It therefore does not
depend on Python dictionary insertion order, process randomization, or clocks.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np


def json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (Path,)):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def canonical_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=json_default,
    ).encode("utf-8")


def canonical_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_with_checksum(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with ``checksum`` computed over all other fields."""

    body = {key: value for key, value in payload.items() if key != "checksum"}
    result = dict(body)
    result["checksum"] = canonical_hash(body)
    return result


def verify_checksum(document: dict[str, Any]) -> bool:
    expected = document.get("checksum")
    if not isinstance(expected, str):
        return False
    body = {key: value for key, value in document.items() if key != "checksum"}
    return canonical_hash(body) == expected
