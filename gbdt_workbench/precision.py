from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any

import numpy as np


SUPPORTED_PRECISION_TIERS = ("float32", "float64")


class PrecisionContext:
    """Arithmetic helpers that make operation order explicit.

    The configured tier is the model arithmetic.  The reference tier is the
    next portable tier: float32 -> float64, float64 -> Decimal.  Decimal is
    used only for scalar reference accumulations in the diagnostics path.
    """

    def __init__(self, tier: str) -> None:
        if tier not in SUPPORTED_PRECISION_TIERS:
            raise ValueError(f"unsupported precision tier: {tier}")
        self.tier = tier
        if tier == "float32":
            self.dtype = np.dtype(np.float32)
            self.reference_tier = "float64"
            self.reference_kind = "binary64"
        else:
            self.dtype = np.dtype(np.float64)
            self.reference_tier = "decimal"
            self.reference_kind = "decimal"
        self.zero = self.cast(0.0)

    def cast(self, value: Any) -> np.floating:
        return self.dtype.type(value)

    def ordered_sum(
        self,
        values: np.ndarray,
        indices: np.ndarray | None = None,
        *,
        dtype: np.dtype | None = None,
    ) -> np.floating:
        """Sequential accumulation in row order, with a cast after every add."""
        target_dtype = np.dtype(dtype) if dtype is not None else self.dtype
        acc = target_dtype.type(0.0)
        iterator = values if indices is None else values[indices]
        for raw in iterator:
            acc = target_dtype.type(acc + target_dtype.type(raw))
        return acc

    def ordered_mean(
        self,
        values: np.ndarray,
        indices: np.ndarray | None = None,
        *,
        dtype: np.dtype | None = None,
    ) -> np.floating:
        count = len(indices) if indices is not None else len(values)
        if count == 0:
            return (np.dtype(dtype) if dtype is not None else self.dtype).type(0.0)
        total = self.ordered_sum(values, indices, dtype=dtype)
        target_dtype = np.dtype(dtype) if dtype is not None else self.dtype
        return target_dtype.type(total / target_dtype.type(count))

    def split_gain_from_sums(
        self,
        left_sum: Any,
        right_sum: Any,
        total_sum: Any,
        left_count: int,
        right_count: int,
        total_count: int,
    ) -> np.floating:
        """Variance-reduction gain with an explicit, stable operation order."""
        d = self.dtype
        left_sum = d.type(left_sum)
        right_sum = d.type(right_sum)
        total_sum = d.type(total_sum)
        left_term = d.type(d.type(left_sum * left_sum) / d.type(left_count))
        right_term = d.type(d.type(right_sum * right_sum) / d.type(right_count))
        total_term = d.type(d.type(total_sum * total_sum) / d.type(total_count))
        return d.type(d.type(left_term + right_term) - total_term)

    def sigmoid(self, raw: np.ndarray) -> np.ndarray:
        raw_array = np.asarray(raw, dtype=self.dtype)
        result = np.empty_like(raw_array)
        positive = raw_array >= self.zero
        if np.any(positive):
            positive_values = raw_array[positive]
            result[positive] = self.cast(1.0) / (
                self.cast(1.0) + self.cast(np.exp(-positive_values))
            )
        if np.any(~positive):
            negative_values = raw_array[~positive]
            exp_values = self.cast(np.exp(negative_values))
            result[~positive] = exp_values / (self.cast(1.0) + exp_values)
        return result

    def initial_binary_state(self, target: np.ndarray) -> dict[str, Any]:
        """Return configured arithmetic and reference initial residuals."""
        y = np.asarray(target)
        n = len(y)
        if n == 0:
            raise ValueError("target must not be empty")

        base_mean = self.ordered_mean(y.astype(self.dtype))
        epsilon = self.cast(1e-6)
        one = self.cast(1.0)
        clipped = self.cast(min(max(base_mean, epsilon), one - epsilon))
        base_score = self.cast(np.log(self.cast(clipped / self.cast(one - clipped))))
        base_probability = self.sigmoid(np.asarray([base_score], dtype=self.dtype))[0]
        residual = self.cast(y.astype(self.dtype) - base_probability)

        if self.reference_kind == "binary64":
            ref_dtype = np.dtype(np.float64)
            ref_mean = self.ordered_mean(y.astype(ref_dtype), dtype=ref_dtype)
            ref_clipped = min(max(ref_mean, 1e-6), 1.0 - 1e-6)
            ref_score = np.float64(np.log(np.float64(ref_clipped / (1.0 - ref_clipped))))
            ref_probability = _binary64_sigmoid(ref_score)
            ref_residual = y.astype(ref_dtype) - ref_probability
        else:
            with localcontext() as context:
                context.prec = 50
                ref_mean = Decimal(int(np.sum(y))) / Decimal(n)
                ref_clipped = min(max(ref_mean, Decimal("1e-6")), Decimal("0.999999"))
                ref_score_decimal = (ref_clipped / (Decimal(1) - ref_clipped)).ln()
                ref_probability_decimal = _decimal_sigmoid(ref_score_decimal)
                ref_score = float(ref_score_decimal)
                ref_probability = float(ref_probability_decimal)
            ref_residual = y.astype(np.float64) - np.float64(ref_probability)

        return {
            "base_mean": float(base_mean),
            "base_score": float(base_score),
            "base_probability": float(base_probability),
            "residual": residual,
            "reference_base_score": float(ref_score),
            "reference_base_probability": float(ref_probability),
            "reference_residual": np.asarray(ref_residual, dtype=np.float64),
        }

    def reference_sum(self, values: np.ndarray, indices: np.ndarray) -> Any:
        if self.reference_kind == "binary64":
            accumulator = np.float64(0.0)
            for raw in values[indices]:
                accumulator = np.float64(accumulator + np.float64(raw))
            return accumulator
        with localcontext() as context:
            context.prec = 50
            accumulator = Decimal(0)
            for raw in values[indices]:
                accumulator += Decimal.from_float(float(raw))
            return accumulator

    def reference_mean(self, values: np.ndarray, indices: np.ndarray) -> float:
        if len(indices) == 0:
            return 0.0
        total = self.reference_sum(values, indices)
        if self.reference_kind == "binary64":
            return float(np.float64(total) / np.float64(len(indices)))
        with localcontext() as context:
            context.prec = 50
            return float(total / Decimal(len(indices)))

    def reference_gain(
        self,
        residual: np.ndarray,
        left_indices: np.ndarray,
        right_indices: np.ndarray,
    ) -> float:
        left_count = len(left_indices)
        right_count = len(right_indices)
        total_count = left_count + right_count
        if left_count == 0 or right_count == 0 or total_count == 0:
            return 0.0
        left_sum = self.reference_sum(residual, left_indices)
        right_sum = self.reference_sum(residual, right_indices)
        total_sum = left_sum + right_sum

        if self.reference_kind == "binary64":
            left_term = np.float64(np.float64(left_sum * left_sum) / np.float64(left_count))
            right_term = np.float64(np.float64(right_sum * right_sum) / np.float64(right_count))
            total_term = np.float64(np.float64(total_sum * total_sum) / np.float64(total_count))
            return float(np.float64(np.float64(left_term + right_term) - total_term))

        with localcontext() as context:
            context.prec = 50
            left_term = (left_sum * left_sum) / Decimal(left_count)
            right_term = (right_sum * right_sum) / Decimal(right_count)
            total_term = (total_sum * total_sum) / Decimal(total_count)
            return float((left_term + right_term) - total_term)

    def reference_scale(self, leaf_value: float, learning_rate: float) -> float:
        if self.reference_kind == "binary64":
            return float(
                np.float64(np.float64(leaf_value) * np.float64(learning_rate))
            )
        with localcontext() as context:
            context.prec = 50
            return float(
                Decimal.from_float(float(leaf_value))
                * Decimal.from_float(float(learning_rate))
            )

    def reference_scale_exact(self, leaf_value: float, learning_rate: float) -> Any:
        if self.reference_kind == "binary64":
            return np.float64(np.float64(leaf_value) * np.float64(learning_rate))
        with localcontext() as context:
            context.prec = 50
            return (
                Decimal.from_float(float(leaf_value))
                * Decimal.from_float(float(learning_rate))
            )

    def reference_zero(self) -> Any:
        return np.float64(0.0) if self.reference_kind == "binary64" else Decimal(0)

    def reference_add(self, accumulator: Any, value: Any) -> Any:
        if self.reference_kind == "binary64":
            return np.float64(np.float64(accumulator) + np.float64(value))
        with localcontext() as context:
            context.prec = 50
            return accumulator + value

    def reference_sigmoid(self, raw_value: Any) -> float:
        if self.reference_kind == "binary64":
            return float(_binary64_sigmoid(np.float64(raw_value)))
        with localcontext() as context:
            context.prec = 50
            decimal_value = (
                raw_value
                if isinstance(raw_value, Decimal)
                else Decimal.from_float(float(raw_value))
            )
            return float(_decimal_sigmoid(decimal_value))

    def trace_float(self, value: Any) -> float:
        return float(value)


def _binary64_sigmoid(value: np.float64) -> np.float64:
    value = np.float64(value)
    if value >= np.float64(0.0):
        denominator = np.float64(1.0) + np.exp(-value)
        return np.float64(np.float64(1.0) / denominator)
    exp_value = np.exp(value)
    denominator = np.float64(1.0) + exp_value
    return np.float64(exp_value / denominator)


def _decimal_sigmoid(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        if value >= Decimal(0):
            return Decimal(1) / (Decimal(1) + (-value).exp())
        exp_value = value.exp()
        return exp_value / (Decimal(1) + exp_value)
