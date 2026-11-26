"""Canonical JSON, hashing and atomic persistence helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np


def normalize_number(value: float, digits: int = 12) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("non-finite number is not canonical")
    if abs(value) < 1e-15:
        return 0.0
    return float(round(value, digits))


def to_plain(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return [normalize_number(v) for v in value.reshape(-1).tolist()]
    if isinstance(value, (np.floating, float)):
        return normalize_number(value)
    if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    return value


def canonical_json(data: Any) -> str:
    return json.dumps(
        to_plain(data),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(data: Any) -> bytes:
    return canonical_json(data).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(data: Any) -> str:
    return sha256_bytes(canonical_bytes(data))


def stable_uint(*parts: Any, bits: int = 64) -> int:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[: bits // 8], "big", signed=False)


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_json(path: str | Path, data: Any) -> str:
    payload = canonical_bytes(data)
    atomic_write_bytes(path, payload)
    return sha256_bytes(payload)


def read_json(path: str | Path) -> Any:
    with open(path, "rb") as handle:
        return json.loads(handle.read())


def first_semantic_diff(left: Any, right: Any, path: str = "$") -> dict[str, Any] | None:
    """Return the first deterministic leaf difference between plain JSON values."""
    if type(left) is not type(right):
        return {"path": path, "left": to_plain(left), "right": to_plain(right)}
    if isinstance(left, dict):
        left_keys = sorted(left)
        right_keys = sorted(right)
        if left_keys != right_keys:
            missing_left = sorted(set(right_keys) - set(left_keys))
            missing_right = sorted(set(left_keys) - set(right_keys))
            return {
                "path": f"{path}.<keys>",
                "left": missing_left,
                "right": missing_right,
            }
        for key in left_keys:
            difference = first_semantic_diff(left[key], right[key], f"{path}.{key}")
            if difference:
                return difference
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return {
                "path": f"{path}.<length>",
                "left": len(left),
                "right": len(right),
            }
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            difference = first_semantic_diff(left_item, right_item, f"{path}[{index}]")
            if difference:
                return difference
        return None
    if left != right:
        return {"path": path, "left": to_plain(left), "right": to_plain(right)}
    return None


def layer_first_diff(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten a first semantic difference to layer key and field, when possible."""
    difference = first_semantic_diff(left, right)
    if difference is None:
        return None
    difference["unknown_field"] = difference["path"]
    layer_key = ""
    field = ""
    prefix = "$.layers."
    if difference["path"].startswith(prefix):
        tail = difference["path"][len(prefix):]
        known_keys = set()
        for value in (left.get("layers"), right.get("layers")):
            if isinstance(value, dict):
                known_keys.update(str(key) for key in value)
        for candidate in sorted(known_keys, key=lambda item: (-len(item), item)):
            if tail == candidate:
                layer_key = candidate
                break
            if tail.startswith(f"{candidate}."):
                layer_key = candidate
                remainder = tail[len(candidate) + 1:]
                for separator in (".", "["):
                    if separator in remainder:
                        remainder = remainder.split(separator, 1)[0]
                field = remainder
                break
    difference["layer_key"] = layer_key or None
    difference["field"] = field or difference["unknown_field"]
    return difference


def tree_digest(root: str | Path, relative_to: str | Path | None = None) -> str:
    base = Path(relative_to or root)
    records: list[str] = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            relative = path.relative_to(base).as_posix()
            records.append(f"{relative}\0{sha256_bytes(path.read_bytes())}")
    return sha256_bytes("\n".join(records).encode("utf-8"))

