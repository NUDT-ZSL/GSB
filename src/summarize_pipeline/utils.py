from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_object(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_zero(value: float) -> float:
    if value == 0.0:
        return 0.0
    return value


def output_float(value: float | int | None, digits: int) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if not math.isfinite(value):
        raise ValueError(f"non-finite output value: {value!r}")
    return normalize_zero(round(float(value), digits))


def round_tree(value: Any, digits: int) -> Any:
    if isinstance(value, float):
        return output_float(value, digits)
    if isinstance(value, dict):
        return {key: round_tree(item, digits) for key, item in value.items()}
    if isinstance(value, list):
        return [round_tree(item, digits) for item in value]
    if isinstance(value, tuple):
        return [round_tree(item, digits) for item in value]
    return value


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_atomic(path: str | Path, value: Any, *, pretty: bool = False) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        text = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
    else:
        text = canonical_json(value) + "\n"
    temporary = target.with_name(target.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def write_jsonl_atomic(path: str | Path, rows: list[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(canonical_json(row) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)

