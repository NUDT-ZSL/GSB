"""Stable serialization, fingerprints, atomic files, and tensor summaries."""

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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tensor_fingerprint(array: np.ndarray) -> str:
    canonical = np.ascontiguousarray(np.asarray(array, dtype="<f8"))
    digest = hashlib.sha256()
    digest.update(canonical_json_bytes({"shape": list(canonical.shape), "dtype": "float64"}))
    digest.update(canonical.tobytes(order="C"))
    return digest.hexdigest()


def json_safe_number(value: float) -> float | str | None:
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return float(value)


def tensor_summary(array: np.ndarray) -> dict[str, Any]:
    data = np.asarray(array, dtype=np.float64)
    flat = data.ravel(order="C")
    finite = flat[np.isfinite(flat)]
    if finite.size:
        finite_min: float | str | None = json_safe_number(float(np.min(finite)))
        finite_max: float | str | None = json_safe_number(float(np.max(finite)))
        finite_sum: float | str | None = json_safe_number(float(np.sum(finite, dtype=np.float64)))
        finite_mean: float | str | None = json_safe_number(float(np.mean(finite, dtype=np.float64)))
    else:
        finite_min = finite_max = finite_sum = finite_mean = None
    return {
        "shape": list(data.shape),
        "dtype": "float64",
        "size": int(data.size),
        "sha256": tensor_fingerprint(data),
        "finite_count": int(finite.size),
        "nan_count": int(np.count_nonzero(np.isnan(flat))),
        "pos_inf_count": int(np.count_nonzero(np.isposinf(flat))),
        "neg_inf_count": int(np.count_nonzero(np.isneginf(flat))),
        "zero_count": int(np.count_nonzero(flat == 0.0)),
        "finite_min": finite_min,
        "finite_max": finite_max,
        "finite_sum": finite_sum,
        "finite_mean": finite_mean,
    }


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        finally:
            raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value) + b"\n")


def atomic_write_npy(path: Path, array: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".npy.tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            np.save(handle, np.asarray(array, dtype=np.float64), allow_pickle=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        finally:
            raise


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def stable_sort_key(index: int, node_id: str) -> str:
    return f"{index:08d}:{node_id}"


def sorted_failures(failures: Iterable[dict[str, Any]], topo_index: dict[str, int]) -> list[dict[str, Any]]:
    return sorted(
        failures,
        key=lambda item: (
            topo_index.get(str(item.get("node_id")), 10**9),
            str(item.get("node_id") or ""),
            str(item.get("code") or ""),
            str(item.get("message") or ""),
            str(item.get("injection_id") or ""),
        ),
    )


def deep_copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
