"""Shared numerical operator semantics for float64, float32 and fixed point."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import numpy as np

from .config import ROUNDING_MODES


def alternate_rounding_mode(mode: str) -> str:
    pairs = {
        "nearest_even": "half_away_from_zero",
        "half_away_from_zero": "nearest_even",
        "toward_zero": "floor",
        "floor": "ceil",
        "ceil": "toward_zero",
    }
    return pairs[mode]


def round_ratio_int(
    numerator: np.ndarray | int,
    denominator: int,
    mode: str = "nearest_even",
) -> np.ndarray:
    """Round an integer ratio exactly using the requested mode.

    The denominator must be positive. The computation is integer-only and is
    therefore independent of host floating-point settings.
    """

    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if mode not in ROUNDING_MODES:
        raise ValueError(f"unknown rounding mode: {mode}")
    values = np.asarray(numerator, dtype=np.int64)
    numerator_abs = np.abs(values)
    quotient, remainder = np.divmod(numerator_abs, np.int64(denominator))
    twice = remainder * 2
    sign = np.where(values < 0, np.int64(-1), np.int64(1))

    if mode == "toward_zero":
        result = sign * quotient
    elif mode == "floor":
        result = np.where(
            values >= 0,
            quotient,
            -(quotient + (remainder > 0).astype(np.int64)),
        )
    elif mode == "ceil":
        result = np.where(
            values >= 0,
            quotient + (remainder > 0).astype(np.int64),
            -quotient,
        )
    elif mode == "half_away_from_zero":
        magnitude = quotient + (twice >= denominator).astype(np.int64)
        result = sign * magnitude
    else:
        magnitude = quotient.copy()
        magnitude += (twice > denominator).astype(np.int64)
        magnitude += (
            (twice == denominator) & ((quotient % 2) == 1)
        ).astype(np.int64)
        result = sign * magnitude
    return result.astype(np.int64, copy=False)


@dataclass(frozen=True)
class Quantizer:
    scale: int
    rounding_mode: str

    def __post_init__(self) -> None:
        if self.scale <= 0:
            raise ValueError("scale must be positive")
        if self.rounding_mode not in ROUNDING_MODES:
            raise ValueError(f"unknown rounding mode: {self.rounding_mode}")

    def quantize(self, values: np.ndarray | float) -> np.ndarray:
        array = np.asarray(values, dtype=np.float64)
        scaled = array * float(self.scale)
        if self.rounding_mode == "nearest_even":
            rounded = np.rint(scaled)
        elif self.rounding_mode == "half_away_from_zero":
            rounded = np.sign(scaled) * np.floor(np.abs(scaled) + 0.5)
        elif self.rounding_mode == "toward_zero":
            rounded = np.trunc(scaled)
        elif self.rounding_mode == "floor":
            rounded = np.floor(scaled)
        else:
            rounded = np.ceil(scaled)
        if not np.all(np.isfinite(rounded)):
            raise ValueError("cannot quantize non-finite values")
        info = np.iinfo(np.int64)
        if np.any(rounded < info.min) or np.any(rounded > info.max):
            raise OverflowError("fixed-point value exceeds int64 range")
        return rounded.astype(np.int64)

    def dequantize(self, values: np.ndarray | int) -> np.ndarray:
        return np.asarray(values, dtype=np.float64) / float(self.scale)

    def represent_float(self, value: float) -> float:
        return float(self.dequantize(self.quantize(value)))


def _chunked_index_groups(indices: Sequence[int], chunk_size: int) -> list[list[int]]:
    ordered = list(indices)
    return [ordered[start : start + chunk_size] for start in range(0, len(ordered), chunk_size)]


class PrecisionEngine:
    """Common training operators with an explicit storage representation."""

    def __init__(self, name: str, *, scale: int = 65536, rounding_mode: str = "nearest_even"):
        if name not in {"float64", "float32", "fixed_point"}:
            raise ValueError(f"unknown precision: {name}")
        self.name = name
        self.is_fixed = name == "fixed_point"
        self.dtype = np.dtype(np.float64 if name == "float64" else np.float32)
        self.scale = int(scale)
        self.rounding_mode = rounding_mode
        self.quantizer = Quantizer(scale, rounding_mode) if self.is_fixed else None

    @property
    def tiny(self) -> float:
        return float(np.finfo(self.dtype).tiny)

    @property
    def eps(self) -> float:
        return float(np.finfo(self.dtype).eps)

    def raw_numeric(self, values: np.ndarray) -> np.ndarray:
        if self.is_fixed:
            assert self.quantizer is not None
            return self.quantizer.quantize(values)
        return np.asarray(values, dtype=self.dtype)

    def standardized_numeric(self, values: np.ndarray) -> np.ndarray:
        return self.raw_numeric(values)

    def to_float(self, values: np.ndarray) -> np.ndarray:
        if self.is_fixed:
            assert self.quantizer is not None
            return self.quantizer.dequantize(values)
        return np.asarray(values, dtype=np.float64)

    def labels(self, values: np.ndarray) -> np.ndarray:
        binary = np.asarray(values, dtype=np.float64)
        if self.is_fixed:
            assert self.quantizer is not None
            return self.quantizer.quantize(binary)
        return binary.astype(self.dtype)

    def indicator(self, values: np.ndarray) -> np.ndarray:
        if self.is_fixed:
            return np.asarray(values, dtype=np.int64) * self.scale
        return np.asarray(values, dtype=self.dtype)

    def bias_value(self) -> int | np.floating:
        return self.scale if self.is_fixed else self.dtype.type(1.0)

    def zeros(self, count: int) -> np.ndarray:
        dtype = np.int64 if self.is_fixed else self.dtype
        return np.zeros(count, dtype=dtype)

    def feature_norm(self, features: np.ndarray) -> float:
        values = self.to_float(features)
        return float(np.linalg.norm(values.reshape(-1)))

    def effective_learning_rate(self, learning_rate: float) -> float:
        if self.is_fixed:
            assert self.quantizer is not None
            return self.quantizer.represent_float(learning_rate)
        return float(np.asarray(learning_rate, dtype=self.dtype))

    def scores(self, params: np.ndarray, features: np.ndarray) -> np.ndarray:
        if features.ndim != 2 or features.shape[1] != params.shape[0]:
            raise ValueError("feature/parameter shape mismatch")
        if self.is_fixed:
            result = np.zeros(features.shape[0], dtype=np.int64)
            for column in range(features.shape[1]):
                product = params[column] * features[:, column]
                result += round_ratio_int(product, self.scale, self.rounding_mode)
            return result
        result = np.zeros(features.shape[0], dtype=self.dtype)
        for column in range(features.shape[1]):
            result = (result + params[column] * features[:, column]).astype(self.dtype, copy=False)
        return result

    def probabilities(self, scores: np.ndarray) -> np.ndarray:
        if self.is_fixed:
            score_float = np.asarray(scores, dtype=np.float64) / float(self.scale)
            probability = self._sigmoid_float64(score_float)
            assert self.quantizer is not None
            result = self.quantizer.quantize(probability)
            return np.clip(result, 0, self.scale)
        return self._sigmoid_dtype(np.asarray(scores, dtype=self.dtype))

    def probabilities_as_float(self, probabilities: np.ndarray) -> np.ndarray:
        if self.is_fixed:
            return np.asarray(probabilities, dtype=np.float64) / float(self.scale)
        return np.asarray(probabilities, dtype=np.float64)

    def gradient(
        self,
        params: np.ndarray,
        features: np.ndarray,
        labels: np.ndarray,
        indices: Sequence[int],
    ) -> np.ndarray:
        selected = np.asarray(list(indices), dtype=np.int64)
        if selected.size == 0:
            return self.zeros(params.shape[0])
        x_part = features[selected, :]
        y_part = labels[selected]
        scores = self.scores(params, x_part)
        probabilities = self.probabilities(scores)

        if self.is_fixed:
            residual = probabilities - y_part
            result = np.zeros(params.shape[0], dtype=np.int64)
            for column in range(x_part.shape[1]):
                per_sample = round_ratio_int(
                    residual * x_part[:, column], self.scale, self.rounding_mode
                )
                result[column] = np.sum(per_sample, dtype=np.int64)
            return result

        residual = (probabilities - y_part).astype(self.dtype, copy=False)
        result = np.zeros(params.shape[0], dtype=self.dtype)
        for column in range(x_part.shape[1]):
            products = (residual * x_part[:, column]).astype(self.dtype, copy=False)
            result[column] = np.sum(products, dtype=self.dtype)
        return result

    def reduce_gradients(self, gradients: Iterable[np.ndarray]) -> np.ndarray:
        records = list(gradients)
        if not records:
            raise ValueError("at least one chunk is required")
        if self.is_fixed:
            accumulator = np.zeros_like(records[0], dtype=np.int64)
            for item in records:
                accumulator += item
            return accumulator
        accumulator = np.zeros_like(records[0], dtype=self.dtype)
        for item in records:
            accumulator = (accumulator + item).astype(self.dtype, copy=False)
        return accumulator

    def reduce_full(self, values: np.ndarray) -> np.ndarray | float:
        if self.is_fixed:
            return np.sum(values, axis=0, dtype=np.int64)
        return np.sum(values, axis=0, dtype=self.dtype)

    def apply_update(
        self,
        params: np.ndarray,
        gradient_sum: np.ndarray,
        sample_count: int,
        regularization_strength: float,
        learning_rate: float,
        regularized_mask: np.ndarray | None = None,
    ) -> np.ndarray:
        if self.is_fixed:
            assert self.quantizer is not None
            gradient_mean = round_ratio_int(gradient_sum, sample_count, self.rounding_mode)
            reg_units = int(self.quantizer.quantize(regularization_strength))
            if regularized_mask is None:
                regularized_mask = np.ones_like(params, dtype=bool)
            reg_gradient = round_ratio_int(
                reg_units * params, self.scale, self.rounding_mode
            )
            reg_gradient = np.where(regularized_mask, reg_gradient, 0)
            total_gradient = gradient_mean + reg_gradient
            lr_units = int(self.quantizer.quantize(learning_rate))
            delta = round_ratio_int(
                lr_units * total_gradient, self.scale, self.rounding_mode
            )
            return (params - delta).astype(np.int64, copy=False)

        if regularized_mask is None:
            regularized_mask = np.ones_like(params, dtype=bool)
        denominator = self.dtype.type(sample_count)
        gradient_mean = (gradient_sum / denominator).astype(self.dtype, copy=False)
        regularization = self.dtype.type(regularization_strength)
        reg_gradient = (regularization * params).astype(self.dtype, copy=False)
        reg_gradient = np.where(regularized_mask, reg_gradient, self.dtype.type(0.0))
        total_gradient = (gradient_mean + reg_gradient).astype(self.dtype, copy=False)
        lr = self.dtype.type(learning_rate)
        delta = (lr * total_gradient).astype(self.dtype, copy=False)
        return (params - delta).astype(self.dtype, copy=False)

    def loss(
        self,
        params: np.ndarray,
        features: np.ndarray,
        labels: np.ndarray,
        *,
        chunks: Sequence[Sequence[int]] | None = None,
        reduce_mode: str = "full",
        include_regularization: bool = True,
        regularization_strength: float = 0.0,
        regularized_mask: np.ndarray | None = None,
    ) -> float:
        if reduce_mode not in {"full", "chunked"}:
            raise ValueError("reduce_mode must be full or chunked")
        all_indices = np.arange(features.shape[0], dtype=np.int64)
        if chunks is None:
            chunks = [list(range(features.shape[0]))]

        if self.is_fixed:
            assert self.quantizer is not None
            per_sample_units = self._fixed_per_sample_loss_units(params, features, labels, all_indices)
            if reduce_mode == "full":
                data_sum = int(np.sum(per_sample_units, dtype=np.int64))
            else:
                data_sum = 0
                for chunk in chunks:
                    chunk_indices = np.asarray(list(chunk), dtype=np.int64)
                    data_sum += int(np.sum(per_sample_units[chunk_indices], dtype=np.int64))
            data_mean_units = int(
                round_ratio_int(data_sum, max(1, features.shape[0]), self.rounding_mode)
            )
            if include_regularization:
                weight_values = self.to_float(params)
                if regularized_mask is None:
                    regularized_mask = np.ones_like(params, dtype=bool)
                regularizer = 0.5 * regularization_strength * float(
                    np.sum(weight_values[regularized_mask] ** 2, dtype=np.float64)
                )
                regularizer_units = int(self.quantizer.quantize(regularizer))
            else:
                regularizer_units = 0
            return float(data_mean_units + regularizer_units) / float(self.scale)

        all_features = features
        all_labels = labels
        scores = self.scores(params, all_features)
        probabilities = self.probabilities(scores).astype(self.dtype, copy=False)
        eps = self.dtype.type(np.finfo(self.dtype).eps)
        clipped = np.clip(probabilities, eps, self.dtype.type(1.0) - eps)
        loss_values = -(
            all_labels * np.log(clipped)
            + (self.dtype.type(1.0) - all_labels) * np.log(self.dtype.type(1.0) - clipped)
        )
        loss_values = loss_values.astype(self.dtype, copy=False)
        if reduce_mode == "full":
            data_sum = np.sum(loss_values, dtype=self.dtype)
        else:
            data_sum = self.dtype.type(0.0)
            for chunk in chunks:
                chunk_indices = np.asarray(list(chunk), dtype=np.int64)
                data_sum = (
                    data_sum + np.sum(loss_values[chunk_indices], dtype=self.dtype)
                ).astype(self.dtype, copy=False)
        data_mean = (data_sum / self.dtype.type(features.shape[0])).astype(self.dtype, copy=False)
        if include_regularization:
            if regularized_mask is None:
                regularized_mask = np.ones_like(params, dtype=bool)
            regularizer = self.dtype.type(regularization_strength) * self.dtype.type(0.5) * np.sum(
                (params[regularized_mask] * params[regularized_mask]).astype(
                    self.dtype, copy=False
                ),
                dtype=self.dtype,
            )
            data_mean = (data_mean + regularizer).astype(self.dtype, copy=False)
        return float(data_mean)

    def validation_metrics(
        self,
        params: np.ndarray,
        features: np.ndarray,
        labels: np.ndarray,
        *,
        regularization_strength: float,
        regularized_mask: np.ndarray | None = None,
    ) -> dict[str, Any]:
        scores = self.scores(params, features)
        probabilities = self.probabilities_as_float(self.probabilities(scores))
        hard = (probabilities >= 0.5).astype(np.int64)
        target = np.asarray(labels, dtype=bool).astype(np.int64)
        correct = int(np.sum(hard == target, dtype=np.int64))
        clipped = np.clip(probabilities, 1e-300, 1.0 - 1e-16)
        log_loss = float(
            -np.mean(target * np.log(clipped) + (1 - target) * np.log(1.0 - clipped))
        )
        return {
            "count": int(features.shape[0]),
            "accuracy": float(correct / max(1, features.shape[0])),
            "log_loss": log_loss,
            "positive_rate": float(np.mean(hard)) if hard.size else 0.0,
            "regularization_strength": float(regularization_strength),
        }

    def fixed_per_sample_loss_units(
        self, params: np.ndarray, features: np.ndarray, labels: np.ndarray
    ) -> np.ndarray:
        return self._fixed_per_sample_loss_units(
            params, features, labels, np.arange(features.shape[0], dtype=np.int64)
        )

    def _fixed_per_sample_loss_units(
        self,
        params: np.ndarray,
        features: np.ndarray,
        labels: np.ndarray,
        indices: np.ndarray,
    ) -> np.ndarray:
        assert self.quantizer is not None
        scores = self.scores(params, features[indices])
        probabilities_units = self.probabilities(scores)
        probability = probabilities_units.astype(np.float64) / float(self.scale)
        probability = np.clip(probability, 1.0 / self.scale, 1.0 - 1.0 / self.scale)
        target = labels[indices].astype(np.float64) / float(self.scale)
        values = -(target * np.log(probability) + (1.0 - target) * np.log(1.0 - probability))
        return self.quantizer.quantize(values)

    def parameter_vector_to_float(self, params: np.ndarray) -> list[float]:
        return [float(item) for item in self.to_float(params)]

    def parameter_vector_from_float(self, values: Sequence[float]) -> np.ndarray:
        return self.raw_numeric(np.asarray(values, dtype=np.float64))

    @staticmethod
    def _sigmoid_float64(values: np.ndarray) -> np.ndarray:
        result = np.empty_like(values, dtype=np.float64)
        positive = values >= 0
        result[positive] = 1.0 / (1.0 + np.exp(-values[positive]))
        exponent = np.exp(values[~positive])
        result[~positive] = exponent / (1.0 + exponent)
        return result

    def _sigmoid_dtype(self, values: np.ndarray) -> np.ndarray:
        result = np.empty_like(values, dtype=self.dtype)
        positive = values >= 0
        one = self.dtype.type(1.0)
        result[positive] = (one / (one + np.exp(-values[positive]))).astype(
            self.dtype, copy=False
        )
        exponent = np.exp(values[~positive]).astype(self.dtype, copy=False)
        result[~positive] = (exponent / (one + exponent)).astype(self.dtype, copy=False)
        return result

    def chunks(self, indices: Sequence[int], chunk_size: int) -> list[list[int]]:
        return _chunked_index_groups(indices, chunk_size)

    def scale_units(self, value: float) -> int:
        if not self.is_fixed:
            raise ValueError("scale units are only defined for fixed point")
        assert self.quantizer is not None
        return int(self.quantizer.quantize(value))

    def dequantize_scalar(self, units: int) -> float:
        if not self.is_fixed:
            raise ValueError("scale units are only defined for fixed point")
        return float(units) / float(self.scale)


def make_engine(
    precision: str,
    *,
    fixed_scale: int,
    rounding_mode: str,
) -> PrecisionEngine:
    return PrecisionEngine(precision, scale=fixed_scale, rounding_mode=rounding_mode)


def stable_sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)
