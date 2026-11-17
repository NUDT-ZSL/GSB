"""Canonical serialization and small numeric helpers."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def pretty_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def display_file(path: str | Path) -> str:
    return Path(path).name


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def locate_line(text: str, token: str, *, default: int = 1) -> int:
    position = text.find(token)
    if position < 0:
        return default
    return text.count("\n", 0, position) + 1
