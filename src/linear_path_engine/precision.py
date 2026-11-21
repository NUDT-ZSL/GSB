"""Shared operator semantics for float64, float32, and scaled fixed point."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import PrecisionConfig
from .errors import E_RESOURCE_LIMIT, EngineError


_INT64_SAFE = 2**62


def _round_scaled(values: np.ndarray, scale: int, rounding: str) -> np.ndarray:
    scaled = np.asarray(values, dtype=np.float64) * float(scale)
    if not np.all(np.isfinite(scaled)):
        raise EngineError(
            E_RESOURCE_LIMIT,
            "fixed-point quantization encountered a non-finite value",
            stage="precision",
        )
    if rounding == "nearest_even":
        rounded = np.rint(scaled)
    elif rounding == "nearest_away":
        rounded = np.sign(scaled) * np.floor(np.abs(scaled) + 0.5)
    elif rounding == "truncate":
        rounded = np.trunc(scaled)
    else:
        raise EngineError(
            E_RESOURCE_LIMIT,
            f"unsupported fixed-point rounding mode {rounding!r}",
            stage="precision",
        )
    if rounded.size and np.max(np.abs(rounded)) >= _INT64_SAFE:
        raise EngineError(
            E_RESOURCE_LIMIT,
            "fixed-point value exceeds safe int64 range",
            stage="precision",
            details={"scale": scale, "rounding": rounding},
        )
    return rounded.astype(np.int64)


def _round_ratio(
    numerator: np.ndarray, denominator: int, rounding: str
) -> np.ndarray:
    ratio = np.asarray(numerator, dtype=np.float64) / float(denominator)
    if rounding == "nearest_even":
        rounded = np.rint(ratio)
    elif rounding == "nearest_away":
        rounded = np.sign(ratio) * np.floor(np.abs(ratio) + 0.5)
    elif rounding == "truncate":
        rounded = np.trunc(ratio)
    else:
        raise EngineError(
            E_RESOURCE_LIMIT,
            f"unsupported fixed-point rounding mode {rounding!r}",
            stage="precision",
        )
    return rounded.astype(np.int64)


@dataclass(frozen=True)
class PrecisionOps:
    """Precision adapter with one mathematical contract for all modes."""

    mode: str
    fixed_scale: int = 1
    fixed_rounding: str = "nearest_even"

    @classmethod
    def from_config(cls, config: PrecisionConfig) -> "PrecisionOps":
        return cls(
            mode=config.mode,
            fixed_scale=config.fixed_scale,
            fixed_rounding=config.fixed_rounding,
        )

    @property
    def dtype(self) -> np.dtype[Any]:
        return np.dtype({"float64": np.float64, "float32": np.float32, "fixed_point": np.int64}[self.mode])

    def encode_matrix(self, matrix: np.ndarray) -> np.ndarray:
        if self.mode == "fixed_point":
            return _round_scaled(matrix, self.fixed_scale, self.fixed_rounding)
        return np.asarray(matrix, dtype=self.dtype)

    def encode_targets(self, targets: np.ndarray) -> np.ndarray:
        if self.mode == "fixed_point":
            return _round_scaled(targets, self.fixed_scale, self.fixed_rounding)
        return np.asarray(targets, dtype=self.dtype)

    def encode_params(self, weights: np.ndarray, intercept: float) -> tuple[np.ndarray, Any]:
        if self.mode == "fixed_point":
            encoded_weights = _round_scaled(weights, self.fixed_scale, self.fixed_rounding)
            encoded_intercept = int(
                _round_scaled(np.asarray([intercept]), self.fixed_scale, self.fixed_rounding)[0]
            )
            return encoded_weights, encoded_intercept
        return np.asarray(weights, dtype=self.dtype), self.dtype.type(intercept)

    def zero_params(self, n_features: int) -> tuple[np.ndarray, Any]:
        if self.mode == "fixed_point":
            return np.zeros(n_features, dtype=np.int64), 0
        return np.zeros(n_features, dtype=self.dtype), self.dtype.type(0.0)

    def decode_params(self, weights: np.ndarray, intercept: Any) -> tuple[np.ndarray, float]:
        if self.mode == "fixed_point":
            scale = float(self.fixed_scale)
            return weights.astype(np.float64) / scale, float(intercept) / scale
        return (
            np.asarray(weights, dtype=np.float64),
            float(np.asarray(intercept, dtype=self.dtype)),
        )

    def _check_fixed_dot(self, matrix: np.ndarray, weights: np.ndarray) -> None:
        if matrix.size == 0 or weights.size == 0:
            return
        max_row_dot = (
            float(np.max(np.abs(matrix)))
            * float(np.max(np.abs(weights)))
            * float(matrix.shape[1])
        )
        if max_row_dot >= _INT64_SAFE:
            raise EngineError(
                E_RESOURCE_LIMIT,
                "fixed-point dot product may exceed safe int64 range",
                stage="precision",
                details={
                    "estimated_max_abs_dot": max_row_dot,
                    "safe_limit": _INT64_SAFE,
                },
            )

    def gradient(
        self,
        matrix: np.ndarray,
        targets: np.ndarray,
        weights: np.ndarray,
        intercept: Any,
        total_samples: int,
    ) -> tuple[np.ndarray, Any, float]:
        """Return (encoded gradient, encoded intercept gradient, real SSE)."""
        if total_samples <= 0:
            raise EngineError(
                E_RESOURCE_LIMIT,
                "gradient normalization requires a positive total sample count",
                stage="precision",
            )
        if self.mode == "fixed_point":
            self._check_fixed_dot(matrix, weights)
            raw_logits = np.dot(matrix, weights)
            logits_q = _round_ratio(raw_logits, self.fixed_scale, self.fixed_rounding) + intercept
            residuals_q = logits_q - targets
            residuals_real = residuals_q.astype(np.float64) / float(self.fixed_scale)
            sse = float(np.dot(residuals_real, residuals_real))
            max_abs_gradient_sum = (
                float(matrix.shape[0])
                * float(np.max(np.abs(matrix)))
                * float(np.max(np.abs(residuals_q)))
            )
            if max_abs_gradient_sum >= _INT64_SAFE:
                raise EngineError(
                    E_RESOURCE_LIMIT,
                    "fixed-point gradient accumulation may exceed safe int64 range",
                    stage="precision",
                    details={
                        "estimated_max_abs_gradient_sum": max_abs_gradient_sum,
                        "safe_limit": _INT64_SAFE,
                    },
                )
            raw_gradient = np.dot(matrix.T, residuals_q)
            gradient_q = _round_ratio(
                raw_gradient,
                total_samples * self.fixed_scale,
                self.fixed_rounding,
            )
            intercept_gradient_q = int(
                _round_ratio(
                    np.asarray([np.sum(residuals_q, dtype=np.int64)]),
                    total_samples,
                    self.fixed_rounding,
                )[0]
            )
            return gradient_q, intercept_gradient_q, sse

        logits = np.dot(matrix, weights) + intercept
        residuals = logits - targets
        sse = float(np.dot(residuals, residuals))
        gradient = np.dot(matrix.T, residuals) / self.dtype.type(total_samples)
        intercept_gradient = (
            np.sum(residuals, dtype=self.dtype) / self.dtype.type(total_samples)
        )
        return (
            np.asarray(gradient, dtype=self.dtype),
            self.dtype.type(intercept_gradient),
            sse,
        )

    def prediction_sse(
        self,
        matrix: np.ndarray,
        targets: np.ndarray,
        weights: np.ndarray,
        intercept: Any,
    ) -> tuple[float, float]:
        """Return (SSE, sum of absolute residuals) with the selected precision."""
        if self.mode == "fixed_point":
            self._check_fixed_dot(matrix, weights)
            raw_logits = np.dot(matrix, weights)
            logits_q = _round_ratio(raw_logits, self.fixed_scale, self.fixed_rounding) + intercept
            residuals_q = logits_q - targets
            residuals_real = residuals_q.astype(np.float64) / float(self.fixed_scale)
            return (
                float(np.dot(residuals_real, residuals_real)),
                float(np.sum(np.abs(residuals_real), dtype=np.float64)),
            )
        logits = np.dot(matrix, weights) + intercept
        residuals = logits - targets
        return (
            float(np.dot(residuals, residuals)),
            float(np.sum(np.abs(residuals), dtype=self.dtype)),
        )

    def apply_proximal_step(
        self,
        weights: np.ndarray,
        intercept: Any,
        gradient: np.ndarray,
        intercept_gradient: Any,
        *,
        step_size: float,
        strength: float,
        l1_ratio: float,
    ) -> tuple[np.ndarray, Any]:
        """Apply one gradient step and the elastic-net proximal operator."""
        if self.mode == "fixed_point":
            step = float(step_size)
            step_weights = _round_scaled(
                np.asarray(weights, dtype=np.float64)
                - step * gradient.astype(np.float64),
                1,
                self.fixed_rounding,
            )
            threshold = step * strength * l1_ratio * float(self.fixed_scale)
            denominator = 1.0 + step * strength * (1.0 - l1_ratio)
            values = np.asarray(step_weights, dtype=np.float64)
            shrunk = np.sign(values) * np.maximum(np.abs(values) - threshold, 0.0)
            proxed = _round_scaled(shrunk / denominator, 1, self.fixed_rounding)
            new_intercept = int(
                _round_scaled(
                    np.asarray([float(intercept) - step * float(intercept_gradient)]),
                    1,
                    self.fixed_rounding,
                )[0]
            )
            return proxed.astype(np.int64, copy=False), new_intercept

        step = self.dtype.type(step_size)
        strength_value = self.dtype.type(strength)
        l1_value = self.dtype.type(l1_ratio)
        plain = np.asarray(weights, dtype=self.dtype) - step * np.asarray(
            gradient, dtype=self.dtype
        )
        threshold = step * strength_value * l1_value
        denominator = self.dtype.type(1.0) + step * strength_value * (
            self.dtype.type(1.0) - l1_value
        )
        proxed = np.sign(plain) * np.maximum(np.abs(plain) - threshold, self.dtype.type(0.0))
        proxed = np.asarray(proxed / denominator, dtype=self.dtype)
        new_intercept = self.dtype.type(
            np.asarray(intercept, dtype=self.dtype)
            - step * np.asarray(intercept_gradient, dtype=self.dtype)
        )
        return proxed, new_intercept

    def lipschitz_and_step(self, matrix: np.ndarray, safety: float) -> tuple[float, float]:
        """Spectral Lipschitz constant of augmented squared loss and step size."""
        n_samples = matrix.shape[0]
        if self.mode == "fixed_point":
            decoded = matrix.astype(np.float64) / float(self.fixed_scale)
        else:
            decoded = np.asarray(matrix, dtype=np.float64)
        gram = np.dot(decoded.T, decoded) / float(n_samples)
        column_means = np.mean(decoded, axis=0)
        augmented = np.empty((gram.shape[0] + 1, gram.shape[1] + 1), dtype=np.float64)
        augmented[:-1, :-1] = gram
        augmented[:-1, -1] = column_means
        augmented[-1, :-1] = column_means
        augmented[-1, -1] = 1.0
        eigenvalues = np.linalg.eigvalsh(augmented)
        lipschitz = float(np.max(eigenvalues))
        if not math.isfinite(lipschitz) or lipschitz <= 0.0:
            raise EngineError(
                E_RESOURCE_LIMIT,
                "invalid Lipschitz estimate for standardized augmented design",
                stage="precision",
                details={"lipschitz": lipschitz},
            )
        return lipschitz, float(safety) / lipschitz

    def parameter_inf_norm_change(
        self,
        previous_weights: np.ndarray,
        previous_intercept: Any,
        new_weights: np.ndarray,
        new_intercept: Any,
    ) -> float:
        if self.mode == "fixed_point":
            scale = float(self.fixed_scale)
            changed_weights = np.max(np.abs(new_weights - previous_weights)) / scale
            changed_intercept = abs(float(new_intercept) - float(previous_intercept)) / scale
            return float(max(changed_weights, changed_intercept))
        previous_weights_float = np.asarray(previous_weights, dtype=np.float64)
        new_weights_float = np.asarray(new_weights, dtype=np.float64)
        return float(
            max(
                np.max(np.abs(new_weights_float - previous_weights_float)),
                abs(float(new_intercept) - float(previous_intercept)),
            )
        )

    def extrapolate(
        self,
        previous_weights: np.ndarray,
        previous_intercept: Any,
        current_weights: np.ndarray,
        current_intercept: Any,
        momentum: float,
    ) -> tuple[np.ndarray, Any]:
        if self.mode == "fixed_point":
            momentum_value = float(momentum)
            candidate_weights = (
                current_weights.astype(np.float64)
                + momentum_value
                * (current_weights.astype(np.float64) - previous_weights.astype(np.float64))
            )
            candidate_intercept = float(current_intercept) + momentum_value * (
                float(current_intercept) - float(previous_intercept)
            )
            rounded_weights = _round_scaled(
                candidate_weights, 1, self.fixed_rounding
            )
            rounded_intercept = int(
                _round_scaled(
                    np.asarray([candidate_intercept]), 1, self.fixed_rounding
                )[0]
            )
            return rounded_weights, rounded_intercept
        momentum_value = self.dtype.type(momentum)
        candidate_weights = np.asarray(current_weights, dtype=self.dtype) + momentum_value * (
            np.asarray(current_weights, dtype=self.dtype)
            - np.asarray(previous_weights, dtype=self.dtype)
        )
        candidate_intercept = self.dtype.type(
            np.asarray(current_intercept, dtype=self.dtype)
            + momentum_value
            * (
                np.asarray(current_intercept, dtype=self.dtype)
                - np.asarray(previous_intercept, dtype=self.dtype)
            )
        )
        return np.asarray(candidate_weights, dtype=self.dtype), candidate_intercept

    def restart_measure(
        self,
        extrapolated_weights: np.ndarray,
        extrapolated_intercept: Any,
        new_weights: np.ndarray,
        new_intercept: Any,
        previous_weights: np.ndarray,
        previous_intercept: Any,
    ) -> float:
        if self.mode == "fixed_point":
            delta_extrapolated = extrapolated_weights.astype(np.float64) - new_weights.astype(
                np.float64
            )
            delta_step = new_weights.astype(np.float64) - previous_weights.astype(np.float64)
            weight_measure = float(np.dot(delta_extrapolated, delta_step))
            intercept_measure = (
                float(extrapolated_intercept) - float(new_intercept)
            ) * (float(new_intercept) - float(previous_intercept))
            return weight_measure + intercept_measure
        delta_extrapolated = np.asarray(extrapolated_weights, dtype=np.float64) - np.asarray(
            new_weights, dtype=np.float64
        )
        delta_step = np.asarray(new_weights, dtype=np.float64) - np.asarray(
            previous_weights, dtype=np.float64
        )
        return float(
            np.dot(delta_extrapolated, delta_step)
            + (float(extrapolated_intercept) - float(new_intercept))
            * (float(new_intercept) - float(previous_intercept))
        )

