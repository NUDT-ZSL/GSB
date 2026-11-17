"""Canonical serialization, hashing, and deterministic numeric helpers."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_text(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ) + "\n"
    return canonical_json_bytes(value).decode("utf-8")


def sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_obj(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def derived_key(*parts: Any) -> str:
    """Derive a stable SHA-256 cache key from typed, canonical parts."""
    return sha256_obj({"parts": list(parts)})


def round_half_away_ratio(numerator: int, denominator: int) -> int:
    """Round an exact integer ratio to nearest, with halves away from zero."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    sign = -1 if numerator < 0 else 1
    absolute = abs(int(numerator))
    quotient, remainder = divmod(absolute, int(denominator))
    if 2 * remainder >= int(denominator):
        quotient += 1
    return sign * quotient


def clamp_int(value: int, lower: int, upper: int) -> int:
    return lower if value < lower else upper if value > upper else value


def round_float_half_away(value: float) -> int:
    """Deterministic half-away-from-zero rounding for a finite float."""
    if not math.isfinite(value):
        raise ValueError("cannot quantize non-finite value")
    if value >= 0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def quantize_float_q(value: float, bits: int) -> int:
    if bits < 0:
        raise ValueError("fractional bits must be non-negative")
    return round_float_half_away(float(value) * float(1 << bits))


def round_significant(value: float, digits: int) -> float:
    """Stable decimal significant-digit rounding; never emits NaN/Inf."""
    if not math.isfinite(value):
        raise ValueError("non-finite feature value")
    if value == 0.0:
        return 0.0
    text = f"{value:.{digits - 1}e}"
    rounded = float(text)
    return 0.0 if rounded == 0.0 else rounded


def rounded_float_list(values: np.ndarray | list[float], digits: int) -> list[float]:
    return [round_significant(float(item), digits) for item in values]


def safe_ratio_float(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def jsonable(value: Any) -> Any:
    """Convert numpy scalar/array values into canonical JSON-compatible values."""
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, np.ndarray):
        return [jsonable(item) for item in value.tolist()]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    return value


def write_json_atomic(path: Path, value: Any, *, pretty: bool = True) -> None:
    """Write JSON through a same-directory temporary file and atomic replace."""
    import os
    import tempfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_text(jsonable(value), pretty=pretty).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except (AttributeError, OSError):
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_json(path: Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)
