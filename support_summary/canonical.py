"""Canonical JSON, hashing, and crash-safe file helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_value(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def fsync_directory(path: Path) -> None:
    """Best-effort directory fsync, available on POSIX and harmless elsewhere."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: Any, newline: bool = True) -> None:
    data = canonical_bytes(value)
    if newline:
        data += b"\n"
    atomic_write_bytes(path, data)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def jsonl_bytes(records: list[Any]) -> bytes:
    if not records:
        return b""
    return b"\n".join(canonical_bytes(record) for record in records) + b"\n"


def first_difference(left: str, right: str) -> Optional[Dict[str, Any]]:
    limit = min(len(left), len(right))
    for index in range(limit):
        if left[index] != right[index]:
            return {
                "position": index,
                "left": left[max(0, index - 20) : index + 20],
                "right": right[max(0, index - 20) : index + 20],
            }
    if len(left) != len(right):
        index = limit
        return {
            "position": index,
            "left": left[max(0, index - 20) : index + 20],
            "right": right[max(0, index - 20) : index + 20],
        }
    return None


def canonical_tuple(value: Tuple[Any, ...]) -> str:
    return canonical_json(list(value))
