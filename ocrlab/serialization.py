"""Canonical bytes and crash-safe file replacement."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def checksum(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def quantize(value: Any, digits: int = 6) -> Any:
    """Round floats recursively so JSON output is stable and compact."""
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {str(key): quantize(item, digits) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [quantize(item, digits) for item in value]
    return value


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.tmp-",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(str(target.parent), os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: str | Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_bytes(value) + b"\n")


def read_json(path: str | Path) -> Any:
    with open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def read_jsonl(path: str | Path) -> list[Any]:
    records: list[Any] = []
    with open(path, "rb") as handle:
        for raw_line in handle:
            if raw_line.strip():
                records.append(json.loads(raw_line.decode("utf-8")))
    return records


def write_jsonl(path: str | Path, records: list[Any]) -> None:
    payload = b"".join(canonical_bytes(record) + b"\n" for record in records)
    atomic_write_bytes(path, payload)

