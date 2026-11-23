"""Canonical serialization, content hashes, and atomic filesystem helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import tempfile
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .errors import BusinessError, E_STATE_INVALID


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    return sha256_hex(canonical_json_bytes(value))


def payload_checksum(payload: Any) -> str:
    return digest(payload)


def bytes_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64_bytes(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"), validate=True)


def f64_b64(values: Iterable[float]) -> str:
    array = np.asarray(list(values), dtype="<f8")
    return bytes_b64(array.tobytes(order="C"))


def b64_f64(data: str) -> np.ndarray:
    return np.frombuffer(b64_bytes(data), dtype="<f8").copy()


def float_text(value: float) -> str:
    """A canonical, round-trippable representation for scores."""
    return format(float(value), ".17g")


def normalize_json(value: Any) -> Any:
    """Convert numpy scalar/array values into stable JSON representations."""
    if isinstance(value, np.ndarray):
        return {
            "__ndarray__": True,
            "dtype": value.dtype.str,
            "shape": list(value.shape),
            "data": bytes_b64(value.tobytes(order="C")),
        }
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(k): normalize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_json(v) for v in value]
    return value


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(path))
        _fsync_directory(path.parent)
    finally:
        if temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass


def atomic_write_json(path: Path, value: Any) -> None:
    data = canonical_json_bytes(value) + b"\n"
    atomic_write_bytes(path, data)


def read_json(
    path: Path,
    default: Any = None,
    error_code: str = E_STATE_INVALID,
    field: str | None = None,
) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(
            error_code,
            "JSON 文件读取或解析失败",
            field=field or str(path),
            details={"path": str(path), "reason": str(exc)},
        ) from exc


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def little_endian_f64_bytes(values: Iterable[float]) -> bytes:
    return struct.pack("<" + "d" * len(values), *[float(v) for v in values])
