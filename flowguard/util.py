"""Deterministic serialization and small numeric helpers."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


TOKEN_SCALE = 1_000_000
HISTOGRAM_MAX_EXPONENT = 20


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def cost_to_units(cost_weight: float) -> int:
    return max(1, math.ceil(cost_weight * TOKEN_SCALE))


def unit_ratio(value: int, scale: int = TOKEN_SCALE) -> float:
    return value / scale


def latency_bin(latency_ms: float, bins: int = 32) -> int:
    if latency_ms <= 0:
        return 0
    exponent = math.log2(1.0 + latency_ms)
    index = int(math.floor(exponent * bins / HISTOGRAM_MAX_EXPONENT))
    return min(max(index, 0), bins - 1)


def latency_bin_bounds(index: int, bins: int = 32) -> tuple[float, float]:
    width = HISTOGRAM_MAX_EXPONENT / bins
    lower_exponent = index * width
    upper_exponent = (index + 1) * width
    lower = float(math.pow(2.0, lower_exponent) - 1.0)
    upper = float(math.pow(2.0, upper_exponent) - 1.0)
    return lower, upper


def stable_ratio_allow(service: str, seq: int, epoch: int, ratio: float) -> bool:
    if ratio <= 0.0:
        return False
    if ratio >= 1.0:
        return True
    material = f"{service}:{seq}:{epoch}".encode("utf-8")
    value = int(hashlib.sha256(material).hexdigest()[:16], 16)
    return value % 10_000 < int(ratio * 10_000)


def sorted_json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
