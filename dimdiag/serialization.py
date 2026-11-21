"""Canonical serialization, checksums, and deterministic array encoding."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from .errors import E_SNAPSHOT_CORRUPT, BusinessError


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def pretty_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_DTYPE_ALIASES = {
    "float64": np.dtype("<f8"),
    "float32": np.dtype("<f4"),
    "int64": np.dtype("<i8"),
    "<f8": np.dtype("<f8"),
    "<f4": np.dtype("<f4"),
    "<i8": np.dtype("<i8"),
}


def encode_array(array: np.ndarray) -> dict[str, Any]:
    value = np.asarray(array)
    if value.dtype == np.dtype(">f8"):
        value = value.astype("<f8")
    elif value.dtype == np.dtype(">f4"):
        value = value.astype("<f4")
    elif value.dtype == np.dtype(">i8"):
        value = value.astype("<i8")
    elif value.dtype == np.float64:
        value = value.astype("<f8", copy=False)
    elif value.dtype == np.float32:
        value = value.astype("<f4", copy=False)
    elif value.dtype == np.int64:
        value = value.astype("<i8", copy=False)
    else:
        raise ValueError(f"unsupported snapshot array dtype: {value.dtype}")
    return {
        "dtype": value.dtype.str,
        "shape": list(value.shape),
        "data_b64": base64.b64encode(value.tobytes(order="C")).decode("ascii"),
    }


def decode_array(encoded: Any, field: str = "array") -> np.ndarray:
    try:
        dtype_text = encoded["dtype"]
        shape = tuple(int(item) for item in encoded["shape"])
        dtype = _DTYPE_ALIASES.get(dtype_text)
        if dtype is None:
            raise ValueError(f"unsupported dtype {dtype_text}")
        raw = base64.b64decode(encoded["data_b64"], validate=True)
        expected = int(np.prod(shape, dtype=np.int64)) * dtype.itemsize
        if len(raw) != expected:
            raise ValueError(f"payload length {len(raw)} != expected {expected}")
        return np.frombuffer(raw, dtype=dtype).reshape(shape).copy()
    except Exception as exc:
        raise BusinessError(
            E_SNAPSHOT_CORRUPT,
            "snapshot array is malformed",
            {"field": field, "reason": str(exc)},
        ) from exc


def array_checksum(array: np.ndarray) -> str:
    encoded = encode_array(np.asarray(array))
    return sha256_json(encoded)


def atomic_write_bytes(path: str | Path, payload: bytes) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def json_safe(value: Any) -> Any:
    """Convert NumPy scalars and arrays into deterministic JSON-safe values."""

    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist())
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, float):
        if not np.isfinite(value):
            return None
        return float(value)
    return value
