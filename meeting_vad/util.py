"""Canonical serialization, hashing, and numeric helpers."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_line(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_value(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def quantize(value: float, digits: int = 12) -> float:
    result = round(float(value), digits)
    return 0.0 if result == 0.0 else result


def quantize_list(values: Iterable[float], digits: int = 12) -> list[float]:
    return [quantize(value, digits) for value in values]


def sample_to_ms(sample: int, sample_rate: int, digits: int = 6) -> float:
    return quantize(sample * 1000.0 / sample_rate, digits)


def ms_to_sample(ms: float, sample_rate: int) -> int:
    return int(round(float(ms) * sample_rate / 1000.0))


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def write_json_atomic(path: Path, value: Any) -> None:
    write_bytes_atomic(path, canonical_json_bytes(value) + b"\n")


def is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def round_half_up(value: float) -> int:
    return int(math.floor(float(value) + 0.5))
