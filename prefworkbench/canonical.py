"""Canonical JSON, content hashing, and atomic local-file helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import E_CACHE_CORRUPT, E_CHECKSUM_MISMATCH, BusinessError


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(value: Any) -> str:
    return "sha256:" + sha256_bytes(canonical_bytes(value))


def sealed(payload: dict[str, Any]) -> dict[str, Any]:
    body = dict(payload)
    body.pop("content_hash", None)
    result = dict(body)
    result["content_hash"] = content_hash(body)
    return result


def verify_sealed(value: Any, *, context: str = "artifact") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BusinessError(E_CACHE_CORRUPT, f"{context} is not a JSON object")
    stored = value.get("content_hash")
    if not isinstance(stored, str) or not stored.startswith("sha256:"):
        raise BusinessError(E_CACHE_CORRUPT, f"{context} has no content hash")
    body = dict(value)
    body.pop("content_hash", None)
    actual = content_hash(body)
    if actual != stored:
        raise BusinessError(
            E_CHECKSUM_MISMATCH,
            f"{context} content hash mismatch",
            details={"stored": stored, "actual": actual},
        )
    return body


def json_bytes(value: Any) -> bytes:
    """Pretty JSON for human-facing files; canonical JSON is used for hashing."""
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def atomic_write_json(path: str | Path, value: Any, *, canonical: bool = False) -> None:
    data = canonical_bytes(value) + b"\n" if canonical else json_bytes(value)
    atomic_write_bytes(path, data)


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_uint64(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big")

