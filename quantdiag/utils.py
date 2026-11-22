from __future__ import annotations

import base64
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np


def to_builtin(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Mapping):
        return {str(k): to_builtin(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_builtin(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        to_builtin(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require_finite_float(value: Any, *, version: str | None, field: str) -> float:
    from .errors import format_error

    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise format_error(
            format_version=version,
            field=field,
            message=f"field {field!r} is not parseable as a number",
        ) from exc
    if not math.isfinite(parsed):
        raise format_error(
            format_version=version,
            field=field,
            message=f"field {field!r} must be a finite number",
        )
    return parsed


def require_int(value: Any, *, version: str | None, field: str) -> int:
    from .errors import format_error

    if isinstance(value, bool):
        raise format_error(
            format_version=version,
            field=field,
            message=f"field {field!r} must be an integer",
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise format_error(
            format_version=version,
            field=field,
            message=f"field {field!r} is not parseable as an integer",
        ) from exc
    if isinstance(value, str) and str(parsed) != value.strip():
        # Accept a leading plus sign but reject values such as "1.5".
        try:
            if float(value) != parsed:
                raise ValueError
        except ValueError as exc:
            raise format_error(
                format_version=version,
                field=field,
                message=f"field {field!r} is not parseable as an integer",
            ) from exc
    return parsed


def require_bool(value: Any, *, version: str | None, field: str) -> bool:
    from .errors import format_error

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    raise format_error(
        format_version=version,
        field=field,
        message=f"field {field!r} is not parseable as a boolean",
    )


def array_bytes(array: np.ndarray) -> bytes:
    return np.ascontiguousarray(array, dtype="<f8").tobytes(order="C")


def array_checksum(array: np.ndarray) -> str:
    canonical = np.ascontiguousarray(array, dtype="<f8")
    header = f"{canonical.shape}|<f8|C;".encode("ascii")
    return sha256_bytes(header + canonical.tobytes(order="C"))


def encode_array(array: np.ndarray) -> str:
    canonical = np.ascontiguousarray(array, dtype="<f8")
    return base64.b64encode(canonical.tobytes(order="C")).decode("ascii")


def decode_array(payload: str, shape: Sequence[int]) -> np.ndarray:
    raw = base64.b64decode(payload.encode("ascii"), validate=True)
    array = np.frombuffer(raw, dtype="<f8").copy()
    return array.reshape(tuple(int(v) for v in shape))


def quantiles(values: np.ndarray, prefix: str = "") -> dict[str, float]:
    flat = np.asarray(values, dtype=np.float64).reshape(-1)
    if flat.size == 0:
        flat = np.array([0.0], dtype=np.float64)
    q = np.quantile(flat, [0.5, 0.9, 0.99, 1.0], method="linear")
    names = ("p50", "p90", "p99", "max")
    return {f"{prefix}{name}" if prefix else name: float(v) for name, v in zip(names, q)}


def max_abs(left: np.ndarray, right: np.ndarray) -> float:
    if left.shape != right.shape:
        return float("inf")
    return float(np.max(np.abs(np.asarray(left, dtype=np.float64) - np.asarray(right, dtype=np.float64))))

