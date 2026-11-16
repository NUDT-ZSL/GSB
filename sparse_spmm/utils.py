"""Small deterministic and cross-platform helpers."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .errors import AtomicWriteError, ConfigError


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


def stable_text_hash(parts: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(struct.pack("<Q", len(encoded)))
        digest.update(encoded)
    return digest.hexdigest()


def derived_seed(seed: int, label: str) -> np.random.SeedSequence:
    label_hash = hashlib.sha256(label.encode("utf-8")).digest()
    words = struct.unpack("<8I", label_hash)
    return np.random.SeedSequence([int(seed), *words])


def as_float(value: Any) -> float:
    result = float(value)
    if not np.isfinite(result):
        raise ValueError(f"non-finite JSON number: {value!r}")
    return result


def jsonable(value: Any) -> Any:
    """Convert NumPy containers to strict-JSON values without NaN/Infinity."""

    if isinstance(value, np.ndarray):
        return [jsonable(item) for item in value.tolist()]
    if isinstance(value, np.generic):
        return jsonable(value.item())
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, float):
        if np.isnan(value) or np.isinf(value):
            return {"non_finite_float": repr(value)}
        return value
    return value


def float32_row_digest(values: np.ndarray) -> str:
    arr = np.asarray(values, dtype=np.float32).reshape(-1)
    return sha256_hex(struct.pack(f"<{arr.size}f", *arr.tolist()))


def float64_row_digest(values: np.ndarray) -> str:
    arr = np.asarray(values, dtype=np.float64).reshape(-1)
    return sha256_hex(struct.pack(f"<{arr.size}d", *arr.tolist()))


def matrix_digest(matrix: np.ndarray, dtype: np.dtype[Any] | type[Any]) -> str:
    arr = np.asarray(matrix, dtype=dtype)
    canonical = np.ascontiguousarray(arr, dtype=np.dtype(dtype).newbyteorder("<"))
    header = struct.pack("<QQ", canonical.shape[0], canonical.shape[1])
    return sha256_hex(header + canonical.tobytes(order="C"))


def atomic_write_json(path: str | os.PathLike[str], payload: Any) -> None:
    """Write JSON through a sibling temp file and an atomic os.replace."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        jsonable(payload),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    fd = -1
    temp_name = ""
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=str(destination.parent),
            text=True,
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            fd = -1
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, destination)
        temp_name = ""
        if os.name != "nt":
            dir_fd = os.open(str(destination.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except OSError as exc:
        raise AtomicWriteError(
            f"failed to atomically write {destination}: {exc}",
            stage="atomic-report-write",
            details={"path": str(destination)},
        ) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def require_mapping(value: Any, *, name: str, stage: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(
            f"{name} must be a JSON object",
            stage=stage,
            details={"actual_type": type(value).__name__},
        )
    return value


def require_positive_int(value: Any, *, name: str, stage: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(
            f"{name} must be a positive integer",
            stage=stage,
            details={"field": name, "value": repr(value)},
        )
    return value

