"""Canonical JSON, deterministic score quantization and checksums."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any

SCORE_DECIMALS = 12
SCORE_TOLERANCE = 10.0 ** (-SCORE_DECIMALS)
CANONICAL_SCHEMA = "json-sort-keys-compact-utf8-v1"


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def quantize_float(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("non-finite value cannot be quantized")
    rounded = round(float(value), SCORE_DECIMALS)
    if rounded == 0.0:
        return 0.0
    return rounded


def json_copy(value: Any) -> Any:
    """Deep copy JSON-compatible data without retaining mutable aliases."""

    return copy.deepcopy(value)
