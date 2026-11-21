"""Canonical JSON and hashing helpers.

All persisted monitoring artifacts use these helpers.  Floats are rounded to
12 decimal places and negative zero is normalized so repeated runs on the
same platform produce byte-identical JSON.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from enum import Enum
from typing import Any


FLOAT_DIGITS = 12


def stable_float(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError(f"non-finite float cannot be canonically serialized: {value!r}")
    rounded = round(float(value), FLOAT_DIGITS)
    if rounded == 0:
        return 0.0
    return rounded


def canonical_obj(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return canonical_obj(dataclasses.asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {
            str(key): canonical_obj(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [canonical_obj(item) for item in value]
    if isinstance(value, float):
        return stable_float(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    item = getattr(value, "item", None)
    if callable(item):
        return canonical_obj(item())
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        canonical_obj(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_obj(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def load_canonical_json(path: str) -> Any:
    with open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))

