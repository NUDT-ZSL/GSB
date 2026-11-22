"""Deterministic int8 quantize / dequantize primitives."""
from __future__ import annotations

import numpy as np

INT8_MIN, INT8_MAX = -128, 127


def quantize_int8(x: np.ndarray, scale: float, zero_point: int) -> np.ndarray:
    """Affine quantization to int8. Deterministic (np.round, banker's)."""
    q = np.round(np.asarray(x, dtype=np.float64) / float(scale)) + int(zero_point)
    return np.clip(q, INT8_MIN, INT8_MAX).astype(np.int8)


def dequantize(q: np.ndarray, scale: float, zero_point: int) -> np.ndarray:
    """Affine dequantization to float64 (the reference-value path)."""
    return (np.asarray(q, dtype=np.float64) - int(zero_point)) * float(scale)


def is_valid_zero_point(zero_point) -> bool:
    return isinstance(zero_point, (int, np.integer)) and \
        INT8_MIN <= int(zero_point) <= INT8_MAX
