"""Canonical JSON, hashing, and lossless float64 array encoding."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import numpy as np


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a value with a stable key ordering and compact separators."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def hash_array_f64(array: np.ndarray) -> str:
    arr = np.ascontiguousarray(array, dtype="<f8")
    digest = hashlib.sha256()
    digest.update(str(tuple(arr.shape)).encode("ascii"))
    digest.update(b"\0")
    digest.update(arr.tobytes(order="C"))
    return "sha256:" + digest.hexdigest()


def encode_array_f64(array: np.ndarray) -> dict[str, Any]:
    arr = np.ascontiguousarray(array, dtype="<f8")
    return {
        "dtype": "<f8",
        "shape": list(arr.shape),
        "data_b64": base64.b64encode(arr.tobytes(order="C")).decode("ascii"),
    }


def decode_array_f64(payload: dict[str, Any]) -> np.ndarray:
    if payload.get("dtype") != "<f8":
        raise ValueError("unsupported encoded dtype")
    shape = tuple(int(item) for item in payload["shape"])
    raw = base64.b64decode(payload["data_b64"], validate=True)
    expected = int(np.prod(shape, dtype=np.int64)) * 8
    if len(raw) != expected:
        raise ValueError("encoded array byte length does not match shape")
    arr = np.frombuffer(raw, dtype="<f8").copy().reshape(shape)
    return np.ascontiguousarray(arr, dtype=np.float64)


def canonical_array_checksum(array: np.ndarray) -> str:
    return sha256_json(encode_array_f64(array))
