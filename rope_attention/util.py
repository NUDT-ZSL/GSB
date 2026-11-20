"""Deterministic serialization and hashing helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
from pathlib import Path
from typing import Any

import numpy as np


def canonical_json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    """Serialize JSON without iteration-order or whitespace ambiguity."""

    if pretty:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
            allow_nan=False,
        )
    else:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    return text.encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def hash_array(array: np.ndarray) -> str:
    """Hash dtype, shape and C-order bytes so views cannot alias identities."""

    contiguous = np.ascontiguousarray(array)
    digest = hashlib.sha256()
    digest.update(contiguous.dtype.str.encode("ascii"))
    digest.update(struct.pack("<I", contiguous.ndim))
    for size in contiguous.shape:
        digest.update(struct.pack("<Q", int(size)))
    digest.update(contiguous.tobytes(order="C"))
    return digest.hexdigest()


def format_float(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("non-finite float")
    return repr(float(value))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write and atomically replace a file, including on Windows."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def load_json_checked(path: Path, *, checksum_key: str = "checksum") -> dict[str, Any]:
    from .errors import E_CACHE_CORRUPT, RopeError

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RopeError(
            E_CACHE_CORRUPT,
            "unable to read cache metadata",
            details={"path": str(path), "reason": str(exc)},
        ) from exc
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RopeError(
            E_CACHE_CORRUPT,
            "cache metadata is not valid JSON",
            details={"path": str(path), "reason": str(exc)},
        ) from exc
    checksum = document.get(checksum_key)
    body = {key: value for key, value in document.items() if key != checksum_key}
    if not isinstance(checksum, str) or checksum != sha256_json(body):
        raise RopeError(
            E_CACHE_CORRUPT,
            "cache metadata checksum mismatch",
            details={"path": str(path), "checksum_key": checksum_key},
        )
    return document


def array_to_payload(array: np.ndarray) -> dict[str, Any]:
    contiguous = np.ascontiguousarray(array)
    return {
        "dtype": contiguous.dtype.str,
        "shape": list(contiguous.shape),
        "data_b64": __import__("base64").b64encode(contiguous.tobytes(order="C")).decode(
            "ascii"
        ),
    }


def payload_to_array(payload: dict[str, Any]) -> np.ndarray:
    import base64

    from .errors import E_CACHE_CORRUPT, RopeError

    try:
        dtype = np.dtype(payload["dtype"])
        shape = tuple(int(item) for item in payload["shape"])
        raw = base64.b64decode(payload["data_b64"], validate=True)
        array = np.frombuffer(raw, dtype=dtype)
        if array.size != int(np.prod(shape, dtype=np.int64)):
            raise ValueError("payload size does not match shape")
        return array.reshape(shape).copy()
    except (KeyError, TypeError, ValueError) as exc:
        raise RopeError(
            E_CACHE_CORRUPT,
            "cache array payload is corrupt",
            details={"reason": str(exc)},
        ) from exc

