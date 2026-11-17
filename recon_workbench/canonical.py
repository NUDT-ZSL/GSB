"""Canonical serialization, hashing, stable floats, and atomic file commits."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from pathlib import Path
from typing import Any


_TEMP_LOCK = threading.Lock()
_TEMP_COUNTER = 0


def canonical_float(value: float, digits: int = 10) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"non-finite float cannot be canonicalized: {value!r}")
    if abs(value) < 0.5 * (10.0 ** (-digits)):
        return 0.0
    rounded = round(value, digits)
    return 0.0 if rounded == 0.0 else rounded


def canonical_json(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ) + "\n"
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def fingerprint(namespace: str, value: Any) -> str:
    return sha256_json({"namespace": namespace, "value": value})


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _next_temp_path(target: Path) -> Path:
    global _TEMP_COUNTER
    with _TEMP_LOCK:
        _TEMP_COUNTER += 1
        ordinal = _TEMP_COUNTER
    thread_id = threading.get_ident()
    return target.with_name(
        f".{target.name}.tmp.{os.getpid()}.{thread_id}.{ordinal}"
    )


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path | str, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _next_temp_path(target)
    try:
        with open(temp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        _fsync_directory(target.parent)
    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


def atomic_write_text(path: Path | str, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path | str, value: Any, *, pretty: bool = True) -> None:
    atomic_write_text(path, canonical_json(value, pretty=pretty))


def atomic_copy_bytes(source: Path | str, target: Path | str) -> None:
    atomic_write_bytes(Path(target), Path(source).read_bytes())
