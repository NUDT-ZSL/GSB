"""Deterministic serialization, hashing and numeric helpers."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

FLOAT_EPS = float(np.finfo(np.float64).eps)
FLOAT_TINY = float(np.finfo(np.float64).tiny)


def to_jsonable(value: Any) -> Any:
    """Convert NumPy/Python values into deterministic JSON-compatible values."""

    if isinstance(value, np.ndarray):
        return [to_jsonable(item) for item in value.tolist()]
    if isinstance(value, np.generic):
        return to_jsonable(value.item())
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float is not JSON-safe: {value!r}")
        return value
    return value


def canonical_json(value: Any) -> str:
    """Canonical compact JSON used for checksums and byte-level comparisons."""

    return json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def array_digest(array: np.ndarray, *, dtype: str = "<f8") -> str:
    """Hash shape and C-order bytes, preserving exact floating-point values."""

    contiguous = np.ascontiguousarray(np.asarray(array, dtype=dtype))
    digest = hashlib.sha256()
    digest.update(str(contiguous.shape).encode("ascii"))
    digest.update(b"|")
    digest.update(contiguous.tobytes(order="C"))
    return digest.hexdigest()


def matrix_digest(array: np.ndarray) -> str:
    return array_digest(np.asarray(array, dtype=np.float64), dtype="<f8")


def hash_payload(payload: Any) -> str:
    return sha256_json(payload)


def guard_subnormal(value: float, enabled: bool) -> float:
    """Flush IEEE-754 subnormal float64 values to signed zero."""

    result = float(value)
    if enabled and 0.0 < abs(result) < FLOAT_TINY:
        return math.copysign(0.0, result)
    return result


def finite_or_zero(value: float) -> float:
    return float(value) if math.isfinite(float(value)) else 0.0


@dataclass
class NeumaierSum:
    """Compensated summation in an explicitly declared left-to-right order."""

    total: float = 0.0
    correction: float = 0.0

    def add(self, value: float) -> None:
        value = float(value)
        updated = self.total + value
        if abs(self.total) >= abs(value):
            self.correction += (self.total - updated) + value
        else:
            self.correction += (value - updated) + self.total
        self.total = updated

    @property
    def value(self) -> float:
        return self.total + self.correction


def ordered_sum(values: Iterable[float], compensated: bool) -> float:
    if not compensated:
        total = 0.0
        for value in values:
            total += float(value)
        return total
    accumulator = NeumaierSum()
    for value in values:
        accumulator.add(value)
    return accumulator.value


def rms(values: Iterable[float]) -> float:
    materialized = [float(value) for value in values]
    if not materialized:
        return 0.0
    return math.sqrt(sum(value * value for value in materialized) / len(materialized))


def mean_abs(values: Iterable[float]) -> float:
    materialized = [abs(float(value)) for value in values]
    if not materialized:
        return 0.0
    return sum(materialized) / len(materialized)


def max_abs(values: Iterable[float]) -> float:
    materialized = [abs(float(value)) for value in values]
    return max(materialized) if materialized else 0.0


def relative_deviation(deviation: float, reference: float) -> float:
    return float(deviation) / max(abs(float(reference)), FLOAT_TINY)


def shape_list(matrix: np.ndarray) -> list[int]:
    return [int(item) for item in np.asarray(matrix).shape]


def ensure_finite(name: str, value: float) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result
