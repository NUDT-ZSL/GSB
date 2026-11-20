"""Precision backends sharing a single operator semantics.

Three backends are provided:

* ``float64`` -- reference arithmetic, plain NumPy float64.
* ``float32`` -- every matvec / dot / accumulate is rounded through float32
  using the configured rounding mode (NumPy itself always uses round-half-even,
  so for half_up/truncate we emulate the rounding explicitly on scalars).
* ``fixed``   -- scaled integer arithmetic.  Values are multiplied by
  ``fixed_scale``, rounded with the configured mode, accumulated in int64 and
  converted back with exact division.

The same high-level sequence (matvec -> chunk dot products -> sequential sum)
is executed by every backend so that precision switches change only the
rounding behaviour, never the algorithm.
"""

from __future__ import annotations

import numpy as np


def round_array(values: np.ndarray, mode: str) -> np.ndarray:
    """Round element-wise with an explicit, deterministic rounding mode."""
    if mode == "half_even":
        return np.round(values)
    if mode == "half_up":
        return np.floor(values + 0.5)
    if mode == "truncate":
        return np.trunc(values)
    raise ValueError(f"unsupported rounding mode: {mode}")


class PrecisionBackend:
    """Stateless arithmetic facade selected by configuration."""

    name = "float64"

    def __init__(self, config):
        self.config = config

    def cast(self, array: np.ndarray) -> np.ndarray:
        return np.asarray(array, dtype=np.float64)

    def matvec(self, matrix: np.ndarray, vector: np.ndarray) -> np.ndarray:
        return matrix @ vector

    def dot(self, left: np.ndarray, right: np.ndarray) -> float:
        return float(np.dot(left, right))

    def sequential_sum(self, values: list[float]) -> float:
        """Explicit left-to-right accumulation (the declared reduction)."""
        total = 0.0
        for value in values:
            total = total + value
        return float(total)

    def scale(self, value: float, factor: float) -> float:
        return value * factor

    def add(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        return left + right


class Float32Backend(PrecisionBackend):
    name = "float32"

    def cast(self, array: np.ndarray) -> np.ndarray:
        return np.asarray(array, dtype=np.float32)

    def matvec(self, matrix: np.ndarray, vector: np.ndarray) -> np.ndarray:
        matrix32 = self.cast(matrix)
        vector32 = self.cast(vector)
        out = np.empty(matrix32.shape[0], dtype=np.float32)
        for row in range(matrix32.shape[0]):
            products = matrix32[row] * vector32
            if self.config.rounding == "half_even":
                out[row] = np.float32(self.sequential_sum(
                    [float(x) for x in products]))
            else:
                out[row] = np.float32(self._rounded_reduction(products))
        return out.astype(np.float64)

    def dot(self, left: np.ndarray, right: np.ndarray) -> float:
        products = self.cast(left) * self.cast(right)
        if self.config.rounding == "half_even":
            return float(np.float32(self.sequential_sum(
                [float(x) for x in products])))
        return float(np.float32(self._rounded_reduction(products)))

    def _rounded_reduction(self, products32: np.ndarray) -> float:
        # Emulate half_up/truncate on float32 by rounding each addition at the
        # float32 ULP of its current magnitude.  NumPy's native cast is only
        # used for half_even (the hardware rounding mode).
        total = np.float32(0.0)
        bits = np.finfo(np.float32)
        for product in products32:
            raw = float(total) + float(product)
            if raw == 0.0 or not np.isfinite(raw):
                total = np.float32(raw)
            elif abs(raw) >= bits.max:
                total = np.float32(raw)
            elif self.config.rounding == "truncate":
                ulp = np.float32(np.nextafter(np.float32(raw),
                                              np.float32(np.inf))
                                 - np.float32(raw))
                total = np.float32(np.floor(raw / float(ulp)) * float(ulp))
            else:
                ulp = np.float32(np.nextafter(np.float32(raw),
                                              np.float32(np.inf))
                                 - np.float32(raw))
                total = np.float32(np.floor(raw / float(ulp) + 0.5)
                                   * float(ulp))
            if abs(total) < bits.tiny and self.config.subnormal_protection:
                total = np.float32(bits.tiny if total >= 0 else -bits.tiny)
        return float(total)

    def sequential_sum(self, values: list[float]) -> float:
        total = np.float32(0.0)
        bits = np.finfo(np.float32)
        for value in values:
            total = np.float32(float(total) + float(value))
            if abs(total) < bits.tiny and self.config.subnormal_protection:
                total = np.float32(bits.tiny if total >= 0 else -bits.tiny)
        return float(total)

    def scale(self, value: float, factor: float) -> float:
        return float(np.float32(float(np.float32(value))
                                * float(np.float32(factor))))

    def add(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        return (self.cast(left) + self.cast(right)).astype(np.float64)


class FixedBackend(PrecisionBackend):
    name = "fixed"

    def __init__(self, config):
        super().__init__(config)
        self.fixed_scale = int(config.fixed_scale)

    def to_fixed(self, value) -> np.ndarray:
        arr = np.asarray(value, dtype=np.float64) * self.fixed_scale
        return round_array(arr, self.config.rounding).astype(np.int64)

    def from_fixed(self, value: np.ndarray) -> np.ndarray:
        return value.astype(np.float64) / self.fixed_scale

    def cast(self, array: np.ndarray) -> np.ndarray:
        return self.from_fixed(self.to_fixed(np.asarray(array, np.float64)))

    def matvec(self, matrix: np.ndarray, vector: np.ndarray) -> np.ndarray:
        m_fixed = self.to_fixed(matrix)
        v_fixed = self.to_fixed(vector)
        out = np.zeros(matrix.shape[0], dtype=np.int64)
        for row in range(matrix.shape[0]):
            total = np.int64(0)
            for product in m_fixed[row] * v_fixed:
                total += np.int64(product)
            out[row] = total
        return self.from_fixed(out)

    def dot(self, left: np.ndarray, right: np.ndarray) -> float:
        products = self.to_fixed(left) * self.to_fixed(right)
        total = np.int64(0)
        for product in products:
            total += np.int64(product)
        return float(total) / (self.fixed_scale * self.fixed_scale)

    def sequential_sum(self, values: list[float]) -> float:
        total = np.int64(0)
        for value in values:
            total += self.to_fixed(value)
        return float(total) / self.fixed_scale

    def scale(self, value: float, factor: float) -> float:
        fixed_value = self.to_fixed(value)
        fixed_factor = self.to_fixed(factor)
        return float(fixed_value * fixed_factor) / (self.fixed_scale ** 2)

    def add(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        return self.from_fixed(self.to_fixed(left) + self.to_fixed(right))


def make_backend(config) -> PrecisionBackend:
    if config.precision == "float64":
        return PrecisionBackend(config)
    if config.precision == "float32":
        return Float32Backend(config)
    if config.precision == "fixed":
        return FixedBackend(config)
    raise ValueError(f"unknown precision: {config.precision}")


def flush_subnormals(array: np.ndarray) -> np.ndarray:
    """Replace subnormal magnitudes with zero (used when protection is off)."""
    if np.issubdtype(array.dtype, np.floating):
        tiny = np.finfo(array.dtype).tiny
    else:
        tiny = np.finfo(np.float64).tiny
    return np.where(np.abs(array) < tiny, 0.0, array)
