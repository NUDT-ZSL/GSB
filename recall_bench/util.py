"""Deterministic hashing, canonical JSON and atomic file replacement helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any


def canon_bytes(obj: Any) -> bytes:
    """Serialize an object to canonical bytes.

    Keys are sorted and tuples/lists keep their order, so structurally equal
    objects always produce identical bytes. Floats are rounded to a fixed
    precision so tiny platform differences cannot break cache identity.
    """
    return json.dumps(
        _normalize(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _normalize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _normalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return obj
        return round(obj, 10)
    return obj


def stable_hash(obj: Any) -> str:
    """Return a stable 16-char fingerprint for any JSON-able object."""
    return hashlib.sha256(canon_bytes(obj)).hexdigest()[:16]


def file_fingerprint(path: str) -> str:
    """Content fingerprint of a file (size + sha256)."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 16)
            if not chunk:
                break
            size += len(chunk)
            h.update(chunk)
    return f"{size:012d}-{h.hexdigest()[:16]}"


def atomic_write_json(path: str, payload: Any) -> None:
    """Write JSON via temp file + os.replace so readers only see full files."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def round_score(value: float, ndigits: int = 10) -> float:
    return round(float(value), ndigits)
