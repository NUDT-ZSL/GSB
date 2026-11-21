"""Deterministic JSON encoding and checksums."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np


def r6(value: float) -> float:
    """Round a scalar to a stable, human-readable precision."""

    rounded = round(float(value), 6)
    return 0.0 if rounded == -0.0 else rounded


def to_builtin(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return to_builtin(value.tolist())
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, float):
        return r6(value)
    if isinstance(value, dict):
        return {str(key): to_builtin(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_builtin(item) for item in value]
    return value


def canonical_bytes(value: Any) -> bytes:
    builtin = to_builtin(value)
    text = json.dumps(
        builtin,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return text.encode("utf-8")


def canonical_text(value: Any, *, indent: int | None = None) -> str:
    if indent is None:
        return canonical_bytes(value).decode("utf-8")
    return json.dumps(
        to_builtin(value),
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        allow_nan=False,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checksum(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def bytes_checksum(data: bytes) -> str:
    return sha256_bytes(data)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        try:
            import os

            os.fsync(stream.fileno())
        except OSError:
            pass
    temporary.replace(path)

