"""Canonical JSON, hashes, and stable numeric helpers."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    """Return the single canonical JSON spelling used for every artifact."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def pretty_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def content_hash(value: Any) -> str:
    return sha256_text(canonical_json(value))


def short_hash(value: Any, length: int = 16) -> str:
    return content_hash(value)[:length]


def q6(value: float) -> float:
    """Quantize scores to six decimals and normalize negative zero."""

    if not math.isfinite(value):
        raise ValueError("non-finite score")
    result = round(float(value), 6)
    return 0.0 if result == 0 else result


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

