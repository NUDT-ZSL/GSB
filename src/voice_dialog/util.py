"""Canonical serialization, hashing, rounding, and atomic file helpers."""

from __future__ import annotations

import hashlib
import json
import math
import numbers
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

_JSON_SUFFIX = ".json"
_thread_lock = threading.Lock()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def bytes_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_float(value: float, digits: int = 6) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("non-finite float is not serializable")
    rounded = round(value, digits)
    if rounded == 0:
        return 0.0
    return rounded


def canonicalize(value: Any, digits: int = 6) -> Any:
    """Convert NumPy scalars and nested values to stable JSON primitives."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        return stable_float(float(value), digits)
    if isinstance(value, dict):
        return {str(key): canonicalize(val, digits) for key, val in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonicalize(item, digits) for item in value]
    if hasattr(value, "tolist"):
        return canonicalize(value.tolist(), digits)
    raise TypeError(f"cannot canonicalize value of type {type(value)!r}")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(path: Path, value: Any) -> None:
    """Write deterministic JSON with fsync and same-directory os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (canonical_json(canonicalize(value)) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def artifact_digest(value: Any) -> str:
    return stable_hash(canonicalize(value))


def safe_relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def sorted_task_keys(values: list[str]) -> list[str]:
    return sorted(values, key=lambda value: (value.lower(), value))
