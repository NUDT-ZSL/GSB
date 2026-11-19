"""Canonical serialization, hashing, deterministic seeds and atomic writes."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from pathlib import Path
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def content_checksum(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def derive_seed(*parts: object) -> int:
    """Return a stable NumPy-compatible non-negative seed, independent of hash()."""

    material = "\x1f".join(str(part) for part in parts).encode("utf-8")
    digest = hashlib.sha256(material).digest()
    return struct.unpack("<Q", digest[:8])[0] & ((1 << 63) - 1)


def envelope_checksum(
    category: str,
    version: int,
    payload: Any,
    schema_version: int,
) -> str:
    return content_checksum(
        {
            "schema_version": schema_version,
            "category": category,
            "version": version,
            "payload": payload,
        }
    )


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write and fsync one complete file, then atomically replace the target."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value) + b"\n")
