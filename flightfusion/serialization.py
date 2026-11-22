"""Canonical JSON and content-addressing helpers."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np


def _normalize(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        if hasattr(value, "as_dict"):
            return _normalize(value.as_dict())
        return _normalize(dataclasses.asdict(value))
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, np.ndarray):
        return _normalize(value.tolist())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float cannot be content-addressed")
        if value == 0.0:
            return 0.0
        return value
    return value


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def digest_mapping(value: dict[str, Any]) -> str:
    return content_hash(value)


def clone_json(value: Any) -> Any:
    return json.loads(canonical_bytes(value).decode("utf-8"))

