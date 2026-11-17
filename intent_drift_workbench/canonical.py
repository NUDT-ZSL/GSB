"""Canonical serialization, hashing, numeric rounding, and atomic I/O."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from decimal import Decimal, ROUND_HALF_UP
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


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quantize(value: float, precision: int) -> float:
    """Round using decimal half-up, independent of binary float tie behavior."""
    if not math.isfinite(value):
        raise ValueError(f"cannot quantize non-finite value: {value!r}")
    quantum = Decimal(1).scaleb(-precision)
    rounded = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    result = float(rounded)
    return 0.0 if result == 0 else result


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, payload: str) -> None:
    atomic_write_bytes(path, payload.encode("utf-8"))


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, canonical_json(value) + "\n")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def sorted_value(value: Any) -> Any:
    """Return a JSON value with mappings recursively key-sorted.

    JSON serialization already sorts keys, but this helper is useful when a
    caller wants the same shape without converting to a string.
    """
    if isinstance(value, dict):
        return {key: sorted_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [sorted_value(item) for item in value]
    return value

