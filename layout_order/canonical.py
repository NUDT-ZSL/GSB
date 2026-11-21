"""Canonical JSON, stable floating-point rounding and atomic file helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any


def round_float(value: float, precision: int = 8) -> float:
    if not math.isfinite(float(value)):
        raise ValueError(f"non-finite float: {value!r}")
    rounded = round(float(value), precision)
    if rounded == 0:
        return 0.0
    return rounded


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


def sha256_obj(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def content_checksum(value: Any) -> str:
    return sha256_obj(value)


def verify_checksum(value: Any, expected: str) -> bool:
    return content_checksum(value) == expected


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: str | Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: str | Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_bytes(value) + b"\n")


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def strip_report_hash(report: dict[str, Any]) -> dict[str, Any]:
    clone = dict(report)
    clone.pop("report_hash", None)
    return clone


def report_hash(report: dict[str, Any]) -> str:
    return sha256_obj(strip_report_hash(report))

