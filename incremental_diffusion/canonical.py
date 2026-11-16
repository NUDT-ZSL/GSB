"""Canonical serialization and content fingerprints."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_json(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def hash_object(value: Any) -> str:
    return sha256_hex(canonical_bytes(value))


def normalized_array(array: np.ndarray) -> np.ndarray:
    """Return the canonical dtype and memory layout used for fingerprints."""
    arr = np.asarray(array)
    if np.issubdtype(arr.dtype, np.floating):
        return np.ascontiguousarray(arr, dtype="<f8")
    if np.issubdtype(arr.dtype, np.integer) or arr.dtype == np.bool_:
        if arr.dtype == np.uint8:
            return np.ascontiguousarray(arr, dtype=np.uint8)
        return np.ascontiguousarray(arr, dtype="<i8")
    raise TypeError(f"unsupported array dtype: {arr.dtype}")


def array_fingerprint(array: np.ndarray) -> str:
    arr = normalized_array(array)
    digest = hashlib.sha256()
    digest.update(canonical_bytes({"shape": list(arr.shape), "dtype": arr.dtype.str}))
    digest.update(arr.tobytes(order="C"))
    return "sha256:" + digest.hexdigest()


def array_content_bytes(array: np.ndarray) -> bytes:
    return normalized_array(array).tobytes(order="C")


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def fsync_directory(path: Path | str) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path | str, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{id(data)}")
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        fsync_directory(target.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path | str, value: Any, *, indent: int | None = 2) -> None:
    if indent is None:
        payload = canonical_json(value).encode("utf-8") + b"\n"
    else:
        payload = json.dumps(
            value,
            sort_keys=True,
            indent=indent,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    atomic_write_bytes(path, payload)

