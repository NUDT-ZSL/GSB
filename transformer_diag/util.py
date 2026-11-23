from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .errors import BusinessError


CANONICAL_SCHEMA = 1


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def canonical_json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=_json_default,
        )
    except (TypeError, ValueError) as exc:
        raise BusinessError(
            "E_POLICY_INVALID",
            f"cannot canonically serialize value: {exc}",
            field="serialization",
        ) from exc
    return text.encode("utf-8")


def canonical_json_text(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(
            value,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
            default=_json_default,
        )
    return canonical_json_bytes(value).decode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_value(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def float_key(value: float) -> str:
    return format(float(value), ".17g")


def finite_float(value: float, *, code: str, field: str, job_id: str | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise BusinessError(code, f"{field} must be a number", job_id=job_id, field=field) from exc
    if not math.isfinite(result):
        raise BusinessError(code, f"{field} must be finite", job_id=job_id, field=field)
    return result


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    ensure_parent(path)
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            tmp_name = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, value: Any, *, pretty: bool = False) -> None:
    atomic_write_text(path, canonical_json_text(value, pretty=pretty) + "\n")


def read_json(path: Path, *, code: str = "E_CACHE_CORRUPT", default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BusinessError(code, f"cannot read valid JSON from {path}: {exc}", field=str(path)) from exc


def npy_bytes(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, array, allow_pickle=False)
    return buffer.getvalue()


def array_descriptor(array: np.ndarray) -> dict[str, Any]:
    contiguous = np.ascontiguousarray(array)
    return {
        "dtype": contiguous.dtype.str,
        "shape": list(contiguous.shape),
        "data_b64": base64.b64encode(contiguous.tobytes(order="C")).decode("ascii"),
    }


def array_from_descriptor(descriptor: dict[str, Any]) -> np.ndarray:
    try:
        dtype = np.dtype(descriptor["dtype"])
        shape = tuple(int(item) for item in descriptor["shape"])
        raw = base64.b64decode(descriptor["data_b64"], validate=True)
        expected = math.prod(shape) * dtype.itemsize if shape else dtype.itemsize
        if len(raw) != expected:
            raise ValueError(f"raw byte count {len(raw)} != expected {expected}")
        array = np.frombuffer(raw, dtype=dtype).copy()
        return array.reshape(shape)
    except (KeyError, TypeError, ValueError, base64.binascii.Error) as exc:
        raise BusinessError(
            "E_CACHE_CORRUPT", f"invalid encoded ndarray: {exc}", field="array"
        ) from exc


def encode_array(array: np.ndarray) -> dict[str, Any]:
    return array_descriptor(np.ascontiguousarray(array))


def decode_array(value: dict[str, Any]) -> np.ndarray:
    return array_from_descriptor(value)


def array_content_hash(array: np.ndarray) -> str:
    return sha256_value(array_descriptor(array))


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def normalize_axis(axis: Any, ndim: int, *, job_id: str | None = None) -> tuple[int, int | None]:
    if not is_int(axis):
        raise BusinessError(
            "E_AXIS_INVALID", "axis must be an integer", job_id=job_id, field="axis"
        )
    original = int(axis)
    normalized = original + ndim if original < 0 else original
    if normalized < 0 or normalized >= ndim:
        raise BusinessError(
            "E_AXIS_INVALID",
            f"axis {original} is out of bounds for rank {ndim}",
            job_id=job_id,
            field="axis",
            details={"axis": original, "rank": ndim},
        )
    return normalized, original


def sanitize_component(value: str) -> str:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
    if not value or any(char not in allowed for char in value) or ".." in value:
        raise BusinessError(
            "E_JOB_INVALID",
            f"unsafe identifier {value!r}",
            job_id=value,
            field="job_id",
        )
    return value


def tail_lines(path: Path, limit: int) -> list[str]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return lines[-limit:]


def relative_to_root(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def clamp_positions(flat_indices: Iterable[int], shape: tuple[int, ...], limit: int = 256) -> list[list[int]]:
    indices = list(flat_indices)
    positions: list[list[int]] = []
    if not shape:
        return positions
    for flat in indices[:limit]:
        positions.append([int(item) for item in np.unravel_index(int(flat), shape)])
    return positions

