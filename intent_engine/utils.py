"""Small deterministic utility functions shared by all stages."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=True,
    )


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def short_hash(value: Any, length: int = 16) -> str:
    return stable_hash(value)[:length]


def hash_bucket(label: str, buckets: int) -> int:
    if buckets <= 0:
        raise ValueError("hash bucket count must be positive")
    digest = hashlib.sha256(label.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % buckets


def quantize(value: float, digits: int) -> float:
    if not math.isfinite(float(value)):
        return float(value)
    # ``round`` is deterministic for binary floats; the explicit float cast
    # prevents a JSON encoder from seeing a Decimal or numpy scalar.
    return float(round(float(value), digits))


def is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def atomic_write_text(path: str | os.PathLike[str], content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: str | os.PathLike[str], value: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n",
    )


def read_json(path: str | os.PathLike[str]) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    return float(value)

