"""Canonical serialization, checksums, and atomic local writes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import BusinessError, E_CONFIG_INVALID


def sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): sanitize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(item) for item in value]
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BusinessError(E_CONFIG_INVALID, "Non-finite number in output", {"field": "json"})
        if value == 0.0:
            return 0.0
    return value


def canonical_json_bytes(value: Any) -> bytes:
    clean = sanitize_json(value)
    text = json.dumps(
        clean,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return text.encode("utf-8") + b"\n"


def checksum_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checksum_value(value: Any) -> str:
    return checksum_bytes(canonical_json_bytes(value))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value))


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BusinessError(
                    E_CONFIG_INVALID,
                    "Invalid JSONL record",
                    {"field": "jsonl", "path": str(path), "line": line_number},
                ) from exc
            if not isinstance(value, dict):
                raise BusinessError(
                    E_CONFIG_INVALID,
                    "JSONL record must be an object",
                    {"field": "jsonl", "path": str(path), "line": line_number},
                )
            rows.append(value)
    return rows


def write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    data = b"".join(canonical_json_bytes(row) for row in rows)
    atomic_write_bytes(path, data)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def round_float(value: float, places: int = 12) -> float:
    return round(float(value), places)

