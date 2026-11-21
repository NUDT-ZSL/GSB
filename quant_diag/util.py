from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

import numpy as np


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def json_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def array_digest(array: np.ndarray, dtype: str = "<f8") -> str:
    contiguous = np.ascontiguousarray(array, dtype=dtype)
    header = canonical_json_bytes(
        {"dtype": dtype, "shape": list(contiguous.shape), "order": "C"}
    )
    digest = hashlib.sha256()
    digest.update(header)
    digest.update(b"\0")
    digest.update(contiguous.tobytes(order="C"))
    return digest.hexdigest()


def int_array_digest(array: np.ndarray, dtype: str = "<i4") -> str:
    contiguous = np.ascontiguousarray(array, dtype=dtype)
    header = canonical_json_bytes(
        {"dtype": dtype, "shape": list(contiguous.shape), "order": "C"}
    )
    digest = hashlib.sha256()
    digest.update(header)
    digest.update(b"\0")
    digest.update(contiguous.tobytes(order="C"))
    return digest.hexdigest()


def ensure_finite(array: np.ndarray, label: str) -> None:
    if not np.isfinite(array).all():
        raise ValueError(f"{label} contains non-finite values")


def float_list(array: np.ndarray) -> list[float]:
    contiguous = np.asarray(array, dtype=np.float64)
    if not np.isfinite(contiguous).all():
        raise ValueError("cannot serialize a non-finite floating point value")
    return contiguous.tolist()


def int_list(array: np.ndarray) -> list[int]:
    return [int(value) for value in np.asarray(array, dtype=np.int64).ravel().tolist()]


def flatten_float_list(values: Iterable[Iterable[float]] | Iterable[float]) -> list[float]:
    result: list[float] = []
    for value in values:
        if isinstance(value, (list, tuple)):
            result.extend(float(item) for item in value)
        else:
            result.append(float(value))
    return result


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value) + b"\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def copy_json(value: Any) -> Any:
    return json.loads(canonical_json_bytes(value).decode("utf-8"))


def relative_error(actual: np.ndarray, reference: np.ndarray) -> float:
    numerator = float(np.linalg.norm(actual - reference))
    denominator = float(np.linalg.norm(reference))
    if denominator == 0.0:
        return 0.0 if numerator == 0.0 else math.inf
    return numerator / denominator
