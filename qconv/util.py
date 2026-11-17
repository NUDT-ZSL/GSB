"""Deterministic hashing, atomic I/O, rounding and quantization helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP
from pathlib import Path
from typing import Any

import numpy as np


ROUNDING_MODES = {
    "half_even": ROUND_HALF_EVEN,
    "half_up": ROUND_HALF_UP,
    "half_down": ROUND_HALF_DOWN,
    "half_away_from_zero": ROUND_HALF_UP,
    "floor": ROUND_FLOOR,
    "ceil": ROUND_CEILING,
    "truncate": ROUND_DOWN,
    "away_from_zero": ROUND_UP,
}


def sanitize_json(value: Any) -> Any:
    """Convert non-finite floats into deterministic strings for hashing."""

    if isinstance(value, float):
        if math.isnan(value):
            return "non-finite:nan"
        if math.isinf(value):
            return "non-finite:inf" if value > 0 else "non-finite:-inf"
        return value
    if isinstance(value, dict):
        return {str(key): sanitize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        sanitize_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def array_digest(array: np.ndarray) -> str:
    value = np.asarray(array)
    if np.issubdtype(value.dtype, np.integer):
        canonical = np.ascontiguousarray(value.astype("<i8", copy=False))
        dtype_name = "int64-le"
    elif np.issubdtype(value.dtype, np.floating):
        canonical = np.ascontiguousarray(value.astype("<f8", copy=False))
        dtype_name = "float64-le"
    else:
        canonical = np.ascontiguousarray(value)
        dtype_name = str(value.dtype)
    header = canonical_json(
        {"dtype": dtype_name, "shape": list(canonical.shape)}
    ).encode("utf-8")
    digest = hashlib.sha256(header)
    digest.update(b"\0")
    digest.update(canonical.tobytes(order="C"))
    return digest.hexdigest()


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.tmp-", dir=str(destination.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: str | os.PathLike[str], value: Any) -> None:
    payload = json.dumps(
        value,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    atomic_write_bytes(path, payload + b"\n")


def atomic_write_npy(path: str | os.PathLike[str], array: np.ndarray) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.tmp-", dir=str(destination.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            np.save(handle, array, allow_pickle=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _decimal_scale(value: float | str | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        return Decimal(value)
    return Decimal(str(value))


def _rounding(mode: str):
    try:
        return ROUNDING_MODES[mode]
    except KeyError as exc:
        raise ValueError(f"unsupported rounding mode: {mode}") from exc


def dequantize_integer_array(
    integer_array: np.ndarray,
    weight_scale: float,
    activation_scale: float,
    rounding_mode: str,
    dequant_decimals: int,
) -> tuple[np.ndarray, dict[str, float]]:
    """Apply exact decimal scale reduction and deterministic rounding.

    The integer tensor is never produced through a floating convolution.  This
    function only performs the required per-element scale reduction.
    """

    factor = _decimal_scale(weight_scale) * _decimal_scale(activation_scale)
    quantum = Decimal(1).scaleb(-dequant_decimals)
    rounding = _rounding(rounding_mode)
    exact_values: list[Decimal] = []
    rounded_values: list[Decimal] = []
    result = np.empty(integer_array.shape, dtype=np.float64)
    flat = np.asarray(integer_array, dtype=np.int64).reshape(-1)
    for index, raw in enumerate(flat):
        exact = Decimal(int(raw)) * factor
        rounded = exact.quantize(quantum, rounding=rounding)
        exact_values.append(exact)
        rounded_values.append(rounded)
        result.reshape(-1)[index] = float(rounded)
    if exact_values:
        errors = [abs(float(a - b)) for a, b in zip(exact_values, rounded_values)]
        error_summary = {
            "max_dequant_rounding_error": max(errors),
            "mean_abs_dequant_rounding_error": sum(errors) / len(errors),
        }
    else:
        error_summary = {
            "max_dequant_rounding_error": 0.0,
            "mean_abs_dequant_rounding_error": 0.0,
        }
    return result, error_summary


def _decimal_from_float(value: float) -> Decimal:
    if not math.isfinite(value):
        raise ValueError("non-finite value cannot be quantized")
    return Decimal(str(value))


def quantize_float_array(
    values: np.ndarray,
    bit_width: int,
    scale: float,
    rounding_mode: str,
) -> tuple[np.ndarray, int]:
    """Quantize a float tensor to signed integers at the requested grid."""

    scale_decimal = _decimal_scale(scale)
    rounding = _rounding(rounding_mode)
    lower = -(1 << (bit_width - 1))
    upper = (1 << (bit_width - 1)) - 1
    result = np.empty(values.shape, dtype=np.int64)
    clamped = 0
    for index, value in enumerate(np.asarray(values, dtype=np.float64).reshape(-1)):
        if not math.isfinite(float(value)):
            raise ValueError("non-finite activation cannot be quantized")
        raw = (_decimal_from_float(float(value)) / scale_decimal).quantize(
            Decimal(1), rounding=rounding
        )
        integer = int(raw)
        if integer < lower:
            integer = lower
            clamped += 1
        elif integer > upper:
            integer = upper
            clamped += 1
        result.reshape(-1)[index] = integer
    return result, clamped


def requantize_integer_array(
    values: np.ndarray,
    source_scale: float,
    target_bit_width: int,
    target_scale: float,
    rounding_mode: str,
) -> tuple[np.ndarray, int]:
    """Re-quantize stored integers between two affine integer grids."""

    source = _decimal_scale(source_scale)
    target = _decimal_scale(target_scale)
    rounding = _rounding(rounding_mode)
    lower = -(1 << (target_bit_width - 1))
    upper = (1 << (target_bit_width - 1)) - 1
    result = np.empty(values.shape, dtype=np.int64)
    clamped = 0
    for index, raw in enumerate(np.asarray(values, dtype=np.int64).reshape(-1)):
        value = Decimal(int(raw)) * source
        integer = int((value / target).quantize(Decimal(1), rounding=rounding))
        if integer < lower:
            integer = lower
            clamped += 1
        elif integer > upper:
            integer = upper
            clamped += 1
        result.reshape(-1)[index] = integer
    return result, clamped


def clamp_integer_array(values: np.ndarray, bit_width: int) -> tuple[np.ndarray, int]:
    lower = -(1 << (bit_width - 1))
    upper = (1 << (bit_width - 1)) - 1
    source = np.asarray(values, dtype=np.int64)
    clamped_mask = (source < lower) | (source > upper)
    result = np.clip(source, lower, upper).astype(np.int64, copy=False)
    return result, int(np.count_nonzero(clamped_mask))


def finite_positive(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0.0


def safe_relative_path(base: Path, path: str) -> Path:
    candidate = (base / path).resolve() if not Path(path).is_absolute() else Path(path)
    return candidate
