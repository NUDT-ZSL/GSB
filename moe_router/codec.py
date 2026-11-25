"""Canonical serialization and hashing helpers."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import numpy as np


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    return value


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, np.ndarray):
        return encode_array(value)
    return _normalize_scalar(value)


def canonical_bytes(value: Any) -> bytes:
    normalized = _normalize(value)
    text = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return text.encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def encode_array(array: np.ndarray) -> dict[str, Any]:
    contiguous = np.ascontiguousarray(array)
    if contiguous.dtype.kind == "f":
        canonical = contiguous.astype("<f8", copy=False)
        dtype = "<f8"
    elif contiguous.dtype.kind in {"i", "u"}:
        canonical = contiguous.astype("<i8", copy=False)
        dtype = "<i8"
    elif contiguous.dtype.kind == "b":
        canonical = contiguous.astype("|b1", copy=False)
        dtype = "|b1"
    else:
        raise TypeError(f"unsupported array dtype: {contiguous.dtype}")
    payload = base64.b64encode(canonical.tobytes(order="C")).decode("ascii")
    return {
        "__ndarray__": {
            "dtype": dtype,
            "shape": list(canonical.shape),
            "order": "C",
            "data": payload,
        }
    }


def decode_array(payload: dict[str, Any]) -> np.ndarray:
    meta = payload.get("__ndarray__")
    if not isinstance(meta, dict):
        raise ValueError("invalid encoded ndarray")
    dtype = np.dtype(meta["dtype"])
    shape = tuple(int(item) for item in meta["shape"])
    raw = base64.b64decode(meta["data"].encode("ascii"), validate=True)
    expected = int(np.prod(shape, dtype=np.int64)) * dtype.itemsize
    if len(raw) != expected:
        raise ValueError("encoded ndarray byte length mismatch")
    return np.frombuffer(raw, dtype=dtype).reshape(shape).copy()


def decode_value(value: Any) -> Any:
    if isinstance(value, dict):
        if "__ndarray__" in value:
            return decode_array(value)
        return {key: decode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    return value


def array_digest(array: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(array)
    header = canonical_bytes(
        {
            "dtype": contiguous.dtype.str,
            "shape": list(contiguous.shape),
            "order": "C",
        }
    )
    digest = hashlib.sha256(header)
    digest.update(contiguous.tobytes(order="C"))
    return digest.hexdigest()


def array_to_list(array: np.ndarray) -> list[Any]:
    return np.asarray(array).tolist()
