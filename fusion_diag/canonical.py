from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def digest_file(path: str | Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp.{id(data)}")
    temporary.write_bytes(data)
    temporary.replace(target)


def atomic_write_json(path: str | Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_bytes(value) + b"\n")


def stable_float(value: float, *, digits: int = 12) -> float:
    if not math.isfinite(value):
        raise ValueError(f"non-finite score: {value}")
    tiny = 2.2250738585072014e-308
    if abs(value) < tiny:
        return 0.0
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0 else rounded


def stable_sum(values: list[float], *, digits: int = 12) -> float:
    return stable_float(math.fsum(values), digits=digits)

