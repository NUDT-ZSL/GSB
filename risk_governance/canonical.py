"""Canonical JSON, hashing and atomic filesystem helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

import numpy as np


def to_native(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): to_native(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_native(item) for item in value]
    if isinstance(value, np.generic):
        return to_native(value.item())
    if isinstance(value, np.ndarray):
        return [to_native(item) for item in value.tolist()]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def canonical_dumps(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(
        to_native(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":") if indent is None else (",", ": "),
        indent=indent,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: str | Path, value: Any, *, indent: int = 2) -> None:
    atomic_write_text(path, canonical_dumps(value, indent=indent) + "\n")


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_jsonl(path: str | Path, rows: Iterable[Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [canonical_dumps(row) for row in rows]
    atomic_write_text(target, "".join(line + "\n" for line in lines))


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped:
                continue
            row = json.loads(stripped)
            if not isinstance(row, dict):
                raise ValueError(f"JSONL line {line_no} is not an object")
            row["_line_no"] = line_no
            row["_raw_line"] = stripped
            row["_line_hash"] = sha256_text(stripped)
            rows.append(row)
    return rows


_NATURAL_PART = re.compile(r"(\d+)")


def natural_key(value: str) -> list[Any]:
    return [
        int(part) if part.isdigit() else part
        for part in _NATURAL_PART.split(str(value))
        if part
    ]


def batch_sort_key(row: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    return natural_key(row.get("batch_id", "")), natural_key(row.get("cycle_id", ""))


def ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
