from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .errors import BusinessError, E_BAD_REQUEST


SUPPORTED_PRECISIONS = ("float64", "float32", "fixed")
SUPPORTED_ROUNDING = ("half_even", "half_up", "trunc")


def _round_fixed(values: np.ndarray, mode: str) -> np.ndarray:
    if mode == "half_even":
        return np.rint(values)
    if mode == "half_up":
        return np.floor(values + 0.5)
    if mode == "trunc":
        return np.trunc(values)
    raise BusinessError(
        E_BAD_REQUEST,
        f"unsupported fixed rounding mode: {mode}",
        {"supported": list(SUPPORTED_ROUNDING)},
    )


@dataclass(frozen=True)
class PrecisionSpec:
    name: str
    fixed_scale: int
    fixed_rounding: str

    @classmethod
    def create(
        cls,
        name: str,
        fixed_scale: int = 4096,
        fixed_rounding: str = "half_even",
    ) -> "PrecisionSpec":
        if name not in SUPPORTED_PRECISIONS:
            raise BusinessError(
                E_BAD_REQUEST,
                f"unsupported precision: {name}",
                {"supported": list(SUPPORTED_PRECISIONS)},
            )
        if int(fixed_scale) < 2:
            raise BusinessError(
                E_BAD_REQUEST,
                "fixed_scale must be at least 2",
                {"fixed_scale": fixed_scale},
            )
        if fixed_rounding not in SUPPORTED_ROUNDING:
            raise BusinessError(
                E_BAD_REQUEST,
                f"unsupported fixed rounding mode: {fixed_rounding}",
                {"supported": list(SUPPORTED_ROUNDING)},
            )
        return cls(name, int(fixed_scale), fixed_rounding)


class Executor:
    """One operator surface shared by all precision tiers."""

    def __init__(self, spec: PrecisionSpec) -> None:
        self.spec = spec
        self.scale = np.float64(spec.fixed_scale)
        if spec.name == "float64":
            self.dtype = np.dtype(np.float64)
        elif spec.name == "float32":
            self.dtype = np.dtype(np.float32)
        else:
            self.dtype = np.dtype(np.float64)

    @property
    def is_fixed(self) -> bool:
        return self.spec.name == "fixed"

    def _quantize_int(self, values: np.ndarray) -> np.ndarray:
        scaled = np.asarray(values, dtype=np.float64) * float(self.scale)
        return _round_fixed(scaled, self.spec.fixed_rounding).astype(np.int64)

    def _dequantize_int(self, values: np.ndarray) -> np.ndarray:
        return np.asarray(values, dtype=np.float64) / float(self.scale)

    def cast(self, values: float | np.ndarray) -> np.ndarray | float:
        if np.isscalar(values):
            scalar = np.float64(values)
            if self.is_fixed:
                return float(self._dequantize_int(self._quantize_int(np.asarray(scalar))))
            return self.dtype.type(scalar).item()
        array = np.asarray(values)
        if self.is_fixed:
            return self._dequantize_int(self._quantize_int(array))
        return array.astype(self.dtype, copy=False)

    def add(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        if self.is_fixed:
            return self._dequantize_int(
                self._quantize_int(left) + self._quantize_int(right)
            )
        return (np.asarray(left, dtype=self.dtype) + np.asarray(right, dtype=self.dtype)).astype(
            self.dtype, copy=False
        )

    def sub(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        if self.is_fixed:
            return self._dequantize_int(
                self._quantize_int(left) - self._quantize_int(right)
            )
        return (np.asarray(left, dtype=self.dtype) - np.asarray(right, dtype=self.dtype)).astype(
            self.dtype, copy=False
        )

    def mul(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        if self.is_fixed:
            left_q = self._quantize_int(left)
            right_q = self._quantize_int(right)
            product = np.asarray(left_q, dtype=np.int64) * np.asarray(right_q, dtype=np.int64)
            rounded = _round_fixed(
                product.astype(np.float64) / float(self.scale),
                self.spec.fixed_rounding,
            ).astype(np.int64)
            return self._dequantize_int(rounded)
        return (np.asarray(left, dtype=self.dtype) * np.asarray(right, dtype=self.dtype)).astype(
            self.dtype, copy=False
        )

    def div(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        if self.is_fixed:
            left_q = self._quantize_int(left)
            right_q = self._quantize_int(right)
            guarded = np.where(right_q == 0, 1, right_q)
            quotient = (
                left_q.astype(np.float64) * float(self.scale) / guarded.astype(np.float64)
            )
            rounded = _round_fixed(quotient, self.spec.fixed_rounding).astype(np.int64)
            result = self._dequantize_int(rounded)
            return np.where(right_q == 0, 0.0, result)
        return (np.asarray(left, dtype=self.dtype) / np.asarray(right, dtype=self.dtype)).astype(
            self.dtype, copy=False
        )

    def neg(self, values: np.ndarray) -> np.ndarray:
        return self.sub(np.zeros_like(values, dtype=self.dtype), values)

    def sqrt(self, values: np.ndarray) -> np.ndarray:
        root = np.sqrt(np.asarray(values, dtype=np.float64))
        return np.asarray(self.cast(root), dtype=np.float64)

    def exp(self, values: np.ndarray) -> np.ndarray:
        exp_values = np.exp(np.asarray(values, dtype=np.float64))
        return np.asarray(self.cast(exp_values), dtype=np.float64)

    def clip(self, values: np.ndarray, low: float, high: float) -> np.ndarray:
        clipped = np.clip(np.asarray(values, dtype=np.float64), low, high)
        return np.asarray(self.cast(clipped), dtype=np.float64)

    def mean(self, values: np.ndarray, axis: int | tuple[int, ...] | None = None) -> np.ndarray:
        array = np.asarray(values)
        if self.is_fixed:
            quantized = self._quantize_int(array)
            count = int(np.prod([quantized.shape[i] for i in np.atleast_1d(axis)])) if axis is not None else quantized.size
            total = np.sum(quantized, axis=axis, dtype=np.int64)
            averaged = _round_fixed(
                total.astype(np.float64) / float(max(count, 1)),
                self.spec.fixed_rounding,
            ).astype(np.int64)
            return self._dequantize_int(averaged)
        return np.mean(array.astype(self.dtype, copy=False), axis=axis, dtype=self.dtype)

    def std(self, values: np.ndarray, axis: int | tuple[int, ...] | None = None) -> np.ndarray:
        array = np.asarray(values)
        if self.is_fixed:
            mean = self.mean(array, axis=axis)
            expanded = mean
            if axis is not None:
                expanded = np.expand_dims(mean, axis=axis)
            centered = self.sub(array, expanded)
            squared = self.mul(centered, centered)
            variance = self.mean(squared, axis=axis)
            return self.sqrt(variance)
        cast_array = array.astype(self.dtype, copy=False)
        mean = np.mean(cast_array, axis=axis, dtype=self.dtype)
        expanded = mean
        if axis is not None:
            expanded = np.expand_dims(mean, axis=axis)
        return np.std(cast_array, axis=axis, dtype=self.dtype)

    def sum(self, values: np.ndarray, axis: int | tuple[int, ...] | None = None) -> np.ndarray:
        array = np.asarray(values)
        if self.is_fixed:
            total = np.sum(self._quantize_int(array), axis=axis, dtype=np.int64)
            return self._dequantize_int(total)
        return np.sum(array.astype(self.dtype, copy=False), axis=axis, dtype=self.dtype)

    def matvec3(self, vector: np.ndarray, matrix: np.ndarray) -> np.ndarray:
        """Apply a 3x3 matrix with a fixed, reproducible accumulation order."""

        values = np.asarray(vector)
        mat = np.asarray(matrix, dtype=np.float64)
        outputs = []
        for column in range(3):
            accumulated = self.mul(values[..., 0], float(mat[0, column]))
            accumulated = self.add(
                accumulated, self.mul(values[..., 1], float(mat[1, column]))
            )
            accumulated = self.add(
                accumulated, self.mul(values[..., 2], float(mat[2, column]))
            )
            outputs.append(accumulated)
        return np.stack(outputs, axis=-1)

    def concatenate(self, arrays: Iterable[np.ndarray], axis: int = -1) -> np.ndarray:
        return np.concatenate([np.asarray(x, dtype=self.dtype) for x in arrays], axis=axis)
