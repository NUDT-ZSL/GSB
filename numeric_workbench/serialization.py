"""Canonical serialization and exact array/scalar encoding helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
from typing import Any, Iterable

import numpy as np


def _normalize(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return encode_array(value)
    if isinstance(value, np.generic):
        return _normalize(value.item())
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats are not canonical JSON values")
        return value
    return value


def canonical_dumps(value: Any) -> str:
    return json.dumps(
        _normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def encode_array(array: np.ndarray) -> dict[str, Any]:
    contiguous = np.ascontiguousarray(array)
    return {
        "dtype": contiguous.dtype.str,
        "shape": list(contiguous.shape),
        "data_b64": base64.b64encode(contiguous.tobytes(order="C")).decode("ascii"),
    }


def decode_array(encoded: dict[str, Any]) -> np.ndarray:
    dtype = np.dtype(encoded["dtype"])
    shape = tuple(int(item) for item in encoded["shape"])
    raw = base64.b64decode(encoded["data_b64"].encode("ascii"), validate=True)
    return np.frombuffer(raw, dtype=dtype).reshape(shape).copy()


def float_hex(value: float) -> str:
    return struct.pack(">d", float(value)).hex()


def float_from_hex(value: str) -> float:
    return struct.unpack(">d", bytes.fromhex(value))[0]


def float32_hex(value: float) -> str:
    return struct.pack(">f", float(np.float32(value))).hex()


def float_list_hex(values: Iterable[float]) -> list[str]:
    return [float_hex(item) for item in values]


def percentile_stats(values: Iterable[float]) -> dict[str, float]:
    data = np.asarray(list(values), dtype=np.float64)
    if data.size == 0:
        return {"count": 0, "p50": 0.0, "p90": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}
    return {
        "count": int(data.size),
        "p50": float(np.percentile(data, 50, method="linear")),
        "p90": float(np.percentile(data, 90, method="linear")),
        "p99": float(np.percentile(data, 99, method="linear")),
        "max": float(np.max(data)),
        "mean": float(np.mean(data)),
    }
