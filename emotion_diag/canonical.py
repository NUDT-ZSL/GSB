"""Canonical JSON, hashing and deterministic IO helpers."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np


def to_jsonable(value: Any) -> Any:
    """Convert common NumPy/path objects into JSON-compatible values."""
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        number = float(value)
        return number if np.isfinite(number) else None
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


def canonical_dumps(value: Any) -> str:
    """Serialize data in the stable canonical form used by hashes."""
    return json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value).encode("utf-8")


def sha256_hex(data: bytes | str | Any) -> str:
    digest = hashlib.sha256()
    if isinstance(data, bytes):
        digest.update(data)
    elif isinstance(data, str):
        digest.update(data.encode("utf-8"))
    else:
        digest.update(canonical_bytes(data))
    return digest.hexdigest()


def file_sha256(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: str | Path, value: Any, *, indent: int = 2) -> str:
    text = json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        allow_nan=False,
    )
    atomic_write_bytes(path, (text + "\n").encode("utf-8"))
    return sha256_hex(text + "\n")


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def deep_sizeof(value: Any) -> int:
    """Approximate resident Python object memory without dependencies."""
    seen: set[int] = set()

    def walk(obj: Any) -> int:
        identifier = id(obj)
        if identifier in seen:
            return 0
        seen.add(identifier)
        size = sys.getsizeof(obj)
        if isinstance(obj, dict):
            size += sum(walk(k) + walk(v) for k, v in obj.items())
        elif isinstance(obj, (list, tuple, set, frozenset)):
            size += sum(walk(item) for item in obj)
        return size

    return walk(value)
