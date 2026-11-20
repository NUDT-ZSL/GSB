"""Canonical serialization, hashing and number normalization."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

DECLARED_PRECISION = 12


def qfloat(value: float, precision: int = DECLARED_PRECISION) -> float:
    """Round a float into the workbench's declared JSON precision.

    Negative zero is canonicalized to positive zero so the same semantic value
    cannot produce two byte-level encodings.
    """

    rounded = round(float(value), precision)
    if abs(rounded) < 0.5 * (10.0 ** -precision):
        return 0.0
    return rounded


def qtuple(values: Any, precision: int = DECLARED_PRECISION) -> tuple[float, ...]:
    return tuple(qfloat(v, precision) for v in values)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_hash(*parts: Any) -> str:
    return sha256_value(list(parts))


def logsumexp(values: list[float] | tuple[float, ...]) -> float:
    if not values:
        return -math.inf
    maximum = max(values)
    if maximum == -math.inf:
        return -math.inf
    return maximum + math.log(sum(math.exp(v - maximum) for v in values))

