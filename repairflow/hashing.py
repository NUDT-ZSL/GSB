"""Stable content hashing helpers.

The hashes in this module never include wall-clock time, process ids, temporary
file names, or process-wide random state.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import numpy as np

from .errors import BusinessError, ErrorCode


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        raise TypeError("numpy arrays must be hashed with array_content_hash")
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=_json_default,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def _canonical_array_bytes(array: np.ndarray) -> tuple[dict[str, Any], bytes]:
    arr = np.asarray(array)
    if arr.dtype.hasobject:
        raise TypeError("object arrays are not supported")

    dtype = arr.dtype
    if dtype.itemsize > 1 and dtype.byteorder == ">":
        arr = arr.astype(dtype.newbyteorder("<"), copy=False)
        dtype = arr.dtype

    if dtype.itemsize == 1:
        byteorder = "|"
    else:
        byteorder = "<"

    meta = {
        "dtype": {
            "kind": dtype.kind,
            "itemsize": dtype.itemsize,
            "byteorder": byteorder,
        },
        "shape": [int(v) for v in arr.shape],
    }
    contiguous = np.ascontiguousarray(arr)
    return meta, contiguous.tobytes(order="C")


def array_content_hash(array: np.ndarray) -> str:
    meta, payload = _canonical_array_bytes(np.asarray(array))
    digest = hashlib.sha256()
    digest.update(b"RF-ARRAY-V1\0")
    digest.update(canonical_json(meta).encode("utf-8"))
    digest.update(b"\0")
    digest.update(payload)
    return digest.hexdigest()


def product_hash(array: np.ndarray, metadata: dict[str, Any] | None = None) -> str:
    meta = dict(metadata or {})
    payload = {
        "array_hash": array_content_hash(array),
        "metadata": meta,
    }
    return sha256_json({"schema": "RF-PRODUCT-V1", "payload": payload})


def first_difference(
    left: np.ndarray, right: np.ndarray
) -> tuple[int, int, int, Any, Any] | None:
    """Return row, column, channel, left value, right value for the first diff."""

    if left.shape != right.shape:
        # Compare a flattened common prefix so shape errors still have a location.
        flat_left = np.asarray(left).reshape(-1)
        flat_right = np.asarray(right).reshape(-1)
        limit = min(flat_left.size, flat_right.size)
        for index in range(limit):
            if flat_left[index] != flat_right[index]:
                return (
                    int(index // max(1, left.shape[-1] if left.ndim else 1)),
                    0,
                    0,
                    flat_left[index].item(),
                    flat_right[index].item(),
                )
        return (0, 0, 0, None, None)

    diff = np.argwhere(left != right)
    if not len(diff):
        return None
    row, col = int(diff[0, 0]), int(diff[0, 1])
    if left.ndim == 3:
        channel = int(diff[0, 2])
        return row, col, channel, left[row, col, channel].item(), right[row, col, channel].item()
    return row, col, 0, left[row, col].item(), right[row, col].item()


def validate_finite_number(value: Any, field: str, code: str = ErrorCode.INVALID_CONFIG) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise BusinessError(
            code,
            f"{field} must be a finite number",
            region_id=None,
            field=field,
            value=value,
        )
    number = float(value)
    if not math.isfinite(number):
        raise BusinessError(
            code,
            f"{field} must be finite",
            region_id=None,
            field=field,
            value=value,
        )
    return number
