"""Canonical JSON hashing and atomic local file helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    """Serialize JSON-safe values identically on every supported process.

    No whitespace, stable key order, and UTF-8 are required so hashes and
    recovered artifacts can be compared byte-for-byte.
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def checksum_without(record: dict[str, Any], checksum_field: str) -> str:
    body = {key: value for key, value in record.items() if key != checksum_field}
    return content_hash(body)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write data through an fsynced temporary file and ``os.replace``.

    The destination is either the previous complete file or the new complete
    file; readers never observe a truncated intermediate write.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(str(path.parent), os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> bytes:
    data = canonical_bytes(value) + b"\n"
    atomic_write_bytes(path, data)
    return data


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    data = b"".join(canonical_bytes(record) + b"\n" for record in records)
    atomic_write_bytes(path, data)


def read_json_file(path: Path) -> Any:
    with path.open("rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def safe_workdir_name(value: str) -> str:
    """Map a clip id to a stable filesystem-safe component.

    Raw clip data is currently stored inline, but this helper keeps any
    future per-clip cache layout deterministic.
    """

    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return f"{value[:32]}-{digest}"
