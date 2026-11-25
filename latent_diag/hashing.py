from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def hash_json(value: Any) -> str:
    return hashlib.sha256(b"ldiag-json-v1\0" + canonical_json_bytes(value)).hexdigest()


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(b"ldiag-bytes-v1\0" + data).hexdigest()


def hash_array(array: np.ndarray) -> str:
    arr = np.ascontiguousarray(array)
    dtype_str = arr.dtype.str
    if arr.dtype.byteorder == "=":
        little = arr.dtype.newbyteorder("<")
        arr = np.ascontiguousarray(arr.astype(little, copy=False))
        dtype_str = arr.dtype.str
    header = canonical_json_bytes(
        {
            "dtype": dtype_str,
            "shape": [int(v) for v in arr.shape],
        }
    )
    digest = hashlib.sha256()
    digest.update(b"ldiag-array-v1\0")
    digest.update(len(header).to_bytes(8, "little"))
    digest.update(header)
    digest.update(arr.tobytes(order="C"))
    return digest.hexdigest()


def hash_scalar(value: float | int) -> str:
    return hash_json({"value": value})

