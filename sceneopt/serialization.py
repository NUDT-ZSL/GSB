"""Canonical serialization for reproducible artifacts and checksums."""

from __future__ import annotations

import base64
import hashlib
import json
import math
from typing import Any

import numpy as np

ARRAY_TAG = "__ndarray__"


def _little_endian_array(array: np.ndarray) -> np.ndarray:
    contiguous = np.ascontiguousarray(array)
    dtype = contiguous.dtype
    if dtype.byteorder == ">":
        contiguous = contiguous.astype(dtype.newbyteorder("<"), copy=False)
    elif dtype.byteorder == "=" and np.little_endian is False:
        contiguous = contiguous.astype(dtype.newbyteorder("<"), copy=False)
    return contiguous


def _encode_array(array: np.ndarray) -> dict[str, Any]:
    contiguous = _little_endian_array(array)
    return {
        ARRAY_TAG: True,
        "dtype": contiguous.dtype.str,
        "shape": list(contiguous.shape),
        "data": base64.b64encode(contiguous.tobytes(order="C")).decode("ascii"),
    }


def _decode_array(value: dict[str, Any]) -> np.ndarray:
    dtype = np.dtype(value["dtype"])
    raw = base64.b64decode(value["data"].encode("ascii"), validate=True)
    array = np.frombuffer(raw, dtype=dtype).copy()
    shape = tuple(int(item) for item in value["shape"])
    expected = int(np.prod(shape, dtype=np.int64))
    if array.size != expected:
        raise ValueError(f"array payload has {array.size} values, expected {expected}")
    array = array.reshape(shape)
    if not np.little_endian and array.dtype.byteorder == "<":
        array = array.astype(array.dtype.newbyteorder("="), copy=False)
    return array


def to_jsonable(value: Any) -> Any:
    """Recursively convert values into a deterministic JSON-safe tree."""

    if isinstance(value, np.ndarray):
        return _encode_array(value)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        converted: dict[str, Any] = {}
        for key in sorted(value, key=lambda item: str(item)):
            converted[str(key)] = to_jsonable(value[key])
        return converted
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (str, bool)) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float cannot be serialized: {value!r}")
        return float(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return to_jsonable(value.to_dict())
    raise TypeError(f"unsupported value for canonical serialization: {type(value)!r}")


def from_jsonable(value: Any) -> Any:
    """Decode a canonical JSON-safe tree, restoring arrays."""

    if isinstance(value, dict):
        if value.get(ARRAY_TAG) is True:
            return _decode_array(value)
        return {key: from_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [from_jsonable(item) for item in value]
    return value


def canonical_bytes(value: Any) -> bytes:
    tree = to_jsonable(value)
    text = json.dumps(
        tree,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8")


def canonical_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_checksum(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def load_json_bytes(data: bytes) -> Any:
    return from_jsonable(json.loads(data.decode("utf-8")))


def load_json_file(path: str) -> Any:
    with open(path, "rb") as handle:
        return load_json_bytes(handle.read())


def canonical_json_file(path: str, value: Any) -> bytes:
    data = canonical_bytes(value)
    with open(path, "wb") as handle:
        handle.write(data)
    return data

