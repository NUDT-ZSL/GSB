"""Canonical serialization and hashing helpers.

The cache and lineage layers must not depend on dictionary insertion order,
platform newline conversion, or NumPy's native-endian representation.  Every
persisted structure therefore goes through this module.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np


def _jsonable(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return encode_array(value)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, set):
        return sorted(_jsonable(v) for v in value)
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def hash_value(value: Any) -> str:
    return sha256_hex(canonical_bytes(value))


def encode_array(array: np.ndarray) -> dict[str, Any]:
    """Encode an array in a stable little-endian, C-order representation."""

    arr = np.asarray(array)
    if arr.dtype.byteorder == ">":
        arr = arr.astype(arr.dtype.newbyteorder("<"), copy=False)
    elif arr.dtype.byteorder == "=" and not arr.dtype.isnative:
        arr = arr.astype(arr.dtype.newbyteorder("<"), copy=False)
    arr = np.ascontiguousarray(arr)
    return {
        "dtype": arr.dtype.str,
        "shape": list(arr.shape),
        "data_b64": base64.b64encode(arr.tobytes(order="C")).decode("ascii"),
    }


def decode_array(encoded: dict[str, Any]) -> np.ndarray:
    dtype = np.dtype(encoded["dtype"])
    shape = tuple(int(x) for x in encoded["shape"])
    raw = base64.b64decode(encoded["data_b64"].encode("ascii"))
    return np.frombuffer(raw, dtype=dtype).reshape(shape).copy()


def array_hash(array: np.ndarray) -> str:
    return hash_value(encode_array(np.asarray(array)))


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8", newline="\n") as handle:
        return json.load(handle)


def write_json_atomic(path: str | Path, value: Any) -> None:
    """Write canonical JSON using a same-directory temporary file."""

    import os
    import tempfile

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_bytes(value) + b"\n"
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def write_jsonl(path: str | Path, rows: list[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(canonical_json(row))
            handle.write("\n")


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError as exc:
                from .errors import E_INPUT_INVALID, raise_error

                raise_error(
                    E_INPUT_INVALID,
                    "invalid JSONL row",
                    path=str(path),
                    line=line_number,
                    reason=str(exc),
                )
            if not isinstance(row, dict):
                from .errors import E_INPUT_INVALID, raise_error

                raise_error(
                    E_INPUT_INVALID,
                    "JSONL row must be an object",
                    path=str(path),
                    line=line_number,
                )
            rows.append(row)
    return rows
