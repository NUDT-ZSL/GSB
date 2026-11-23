from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import WorkbenchConfig, KNOWN_ROUNDING_MODES


@dataclass(frozen=True)
class PrecisionMetadata:
    mode: str
    scale: int
    rounding: str
    arithmetic: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "scale": self.scale,
            "rounding": self.rounding,
            "arithmetic": self.arithmetic,
        }


class PrecisionOps:
    """Shared operator surface for float64, float32 and scaled integer modes.

    The fixed mode retains integer accumulations for sums. Non-linear
    operations (exp/log/sqrt/solve) are evaluated after de-quantization and
    their outputs are immediately quantized again using the configured scale
    and rounding mode. This gives every mode the same algebra/order while
    making fixed-point rounding boundaries explicit.
    """

    def __init__(self, config: WorkbenchConfig):
        self.mode = config.precision
        self.scale = int(config.fixed_scale)
        self.rounding = config.fixed_rounding
        self.subnormal_floor = float(config.subnormal_floor)
        if self.mode == "float64":
            self.dtype = np.dtype(np.float64)
            self.arithmetic = "ieee754-binary64"
            self.unit = float(np.finfo(np.float64).eps)
        elif self.mode == "float32":
            self.dtype = np.dtype(np.float32)
            self.arithmetic = "ieee754-binary32"
            self.unit = float(np.finfo(np.float32).eps)
        elif self.mode == "fixed":
            self.dtype = np.dtype(np.float64)
            self.arithmetic = "scaled-int64-accumulation"
            self.unit = 1.0 / float(self.scale)
        else:  # pragma: no cover - config validates before this point
            raise ValueError(f"unsupported precision: {self.mode}")

    @property
    def metadata(self) -> PrecisionMetadata:
        return PrecisionMetadata(
            mode=self.mode,
            scale=self.scale,
            rounding=self.rounding,
            arithmetic=self.arithmetic,
        )

    def _round_array(self, values: np.ndarray) -> np.ndarray:
        if self.rounding == "round_half_even":
            return np.rint(values)
        if self.rounding == "round_half_away_from_zero":
            return np.sign(values) * np.floor(np.abs(values) + 0.5)
        if self.rounding == "floor":
            return np.floor(values)
        if self.rounding == "ceil":
            return np.ceil(values)
        if self.rounding == "trunc":
            return np.trunc(values)
        raise ValueError(f"unsupported rounding mode: {self.rounding}")

    def quantized_int(self, values: Any) -> np.ndarray:
        array = np.asarray(values, dtype=np.float64)
        scaled = self._round_array(array * float(self.scale))
        limit = np.iinfo(np.int64).max
        scaled = np.clip(scaled, -float(limit), float(limit))
        return scaled.astype(np.int64)

    def quantize(self, values: Any) -> np.ndarray:
        array = np.asarray(values)
        if self.mode == "float64":
            result = np.asarray(array, dtype=np.float64)
        elif self.mode == "float32":
            result = np.asarray(array, dtype=np.float32).astype(np.float64)
        else:
            result = self.quantized_int(array).astype(np.float64) / float(self.scale)
        return self.protect(result)

    def cast(self, values: Any) -> np.ndarray:
        return self.quantize(values)

    q = quantize

    def protect(self, values: Any) -> np.ndarray:
        array = np.asarray(values, dtype=np.float64)
        if self.subnormal_floor > 0.0:
            array = np.where(np.abs(array) < self.subnormal_floor, 0.0, array)
        if self.mode == "float64":
            return array.astype(np.float64, copy=False)
        if self.mode == "float32":
            return array.astype(np.float32).astype(np.float64)
        return array

    def sum(
        self,
        values: Any,
        axis: int | tuple[int, ...] | None = None,
        *,
        keepdims: bool = False,
    ) -> np.ndarray:
        array = np.asarray(values)
        if self.mode == "fixed":
            integers = self.quantized_int(array)
            total = np.sum(
                integers, axis=axis, dtype=np.int64, keepdims=keepdims
            )
            return np.asarray(total, dtype=np.float64) / float(self.scale)
        if self.mode == "float64":
            return np.asarray(
                np.sum(array, axis=axis, dtype=np.float64, keepdims=keepdims),
                dtype=np.float64,
            )
        return np.asarray(
            np.sum(array, axis=axis, dtype=np.float32, keepdims=keepdims),
            dtype=np.float32,
        ).astype(np.float64)

    def add(self, left: Any, right: Any) -> np.ndarray:
        if self.mode == "fixed":
            li = self.quantized_int(left)
            ri = self.quantized_int(right)
            return (li.astype(np.float64) + ri.astype(np.float64)) / float(
                self.scale
            )
        return self.quantize(np.asarray(left) + np.asarray(right))

    def sub(self, left: Any, right: Any) -> np.ndarray:
        if self.mode == "fixed":
            li = self.quantized_int(left)
            ri = self.quantized_int(right)
            return (li.astype(np.float64) - ri.astype(np.float64)) / float(
                self.scale
            )
        return self.quantize(np.asarray(left) - np.asarray(right))

    def mul(self, left: Any, right: Any) -> np.ndarray:
        if self.mode == "fixed":
            li = self.quantized_int(left)
            ri = self.quantized_int(right)
            # Both operands are represented as scaled integers. Their product
            # must be divided by scale**2 before it is re-quantized.
            dequantized_product = (
                li.astype(np.float64) * ri.astype(np.float64)
            ) / (float(self.scale) ** 2)
            return self.quantize(dequantized_product)
        return self.quantize(np.asarray(left) * np.asarray(right))

    def div(self, left: Any, right: Any) -> np.ndarray:
        return self.quantize(np.asarray(left) / np.asarray(right))

    def sqrt(self, values: Any) -> np.ndarray:
        return self.quantize(np.sqrt(np.asarray(values, dtype=np.float64)))

    def exp(self, values: Any) -> np.ndarray:
        return self.quantize(np.exp(np.asarray(values, dtype=np.float64)))

    def log(self, values: Any) -> np.ndarray:
        return self.quantize(np.log(np.asarray(values, dtype=np.float64)))

    def max_abs(self, values: Any) -> float:
        array = np.asarray(values, dtype=np.float64)
        if array.size == 0:
            return 0.0
        return float(np.max(np.abs(array)))

    def quantization_bound(self) -> float:
        if self.mode == "fixed":
            return 0.5 / float(self.scale)
        if self.mode == "float32":
            return float(np.finfo(np.float32).eps)
        return float(np.finfo(np.float64).eps)

    def pair_sum(self, left: Any, right: Any) -> np.ndarray:
        return self.add(left, right)


def make_ops(config: WorkbenchConfig) -> PrecisionOps:
    return PrecisionOps(config)


def validate_rounding(mode: str) -> None:
    if mode not in KNOWN_ROUNDING_MODES:
        raise ValueError(f"unknown rounding mode: {mode}")
