"""Small deterministic serialization helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def short_digest(value: Any, length: int = 12) -> str:
    return digest_json(value)[:length]


def ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return (numerator + denominator - 1) // denominator


def ensure_parent(path: str | Path) -> Path:
    result = Path(path)
    result.parent.mkdir(parents=True, exist_ok=True)
    return result


def json_loads(value: str) -> Any:
    return json.loads(value)
