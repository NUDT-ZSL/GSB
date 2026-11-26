"""Self-contained rolling residual statistics and drift scores."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .canonical import sha256_value, stable_float
from .config import StatisticsConfig
from .errors import DriftError, E_CONFIG_INVALID


@dataclass(frozen=True)
class WindowStatistics:
    mean: float
    variance: float
    standard_deviation: float
    skewness: float
    trend: float
    upstream_contribution: float
    input_amplitude: float
    drift_score: float
    effective_sample_count: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "mean": self.mean,
            "variance": self.variance,
            "standard_deviation": self.standard_deviation,
            "skewness": self.skewness,
            "trend": self.trend,
            "upstream_contribution": self.upstream_contribution,
            "input_amplitude": self.input_amplitude,
            "drift_score": self.drift_score,
            "effective_sample_count": self.effective_sample_count,
        }


def _decay_weights(size: int, decay: float) -> np.ndarray:
    indices = np.arange(size, dtype=np.float64)
    return np.power(decay, (size - 1) - indices, dtype=np.float64)


def _weighted_moments(
    values: np.ndarray, weights: np.ndarray
) -> tuple[float, float, float, float, float, float]:
    total = float(np.sum(weights))
    if total <= 0:
        raise DriftError(E_CONFIG_INVALID, "window weights must have positive mass")
    mean = float(np.sum(weights * values) / total)
    centered = values - mean
    variance = float(np.sum(weights * centered * centered) / total)
    effective = float(total * total / np.sum(weights * weights))

    positions = np.arange(values.size, dtype=np.float64)
    position_mean = float(np.sum(weights * positions) / total)
    position_centered = positions - position_mean
    position_variance = float(
        np.sum(weights * position_centered * position_centered) / total
    )
    if position_variance > 0:
        covariance = float(
            np.sum(weights * position_centered * centered) / total
        )
        slope = covariance / position_variance
    else:
        slope = 0.0
    trend = slope * max(1, values.size - 1)
    return mean, variance, slope, trend, effective, total


def compute_upstream_contributions(
    feature_deltas: np.ndarray,
    coefficients: Iterable[float],
    input_scale: float,
) -> np.ndarray:
    deltas = np.asarray(feature_deltas, dtype=np.float64)
    coeffs = np.asarray(tuple(coefficients), dtype=np.float64)
    if deltas.ndim == 1:
        deltas = deltas.reshape(-1, 1)
    if deltas.shape[1] != coeffs.size:
        raise DriftError(
            E_CONFIG_INVALID,
            "feature delta width does not match predictor coefficients",
        )
    if input_scale <= 0:
        raise DriftError(E_CONFIG_INVALID, "input_scale must be positive")
    return np.abs(deltas) @ np.abs(coeffs) / float(input_scale)


def compute_window_statistics(
    raw_residuals: Iterable[float],
    *,
    feature_deltas: Iterable[Iterable[float]],
    coefficients: Iterable[float],
    config: StatisticsConfig,
) -> WindowStatistics:
    residuals = np.asarray(tuple(raw_residuals), dtype=np.float64)
    deltas = np.asarray(tuple(tuple(row) for row in feature_deltas), dtype=np.float64)
    if residuals.ndim != 1 or residuals.size == 0:
        raise DriftError(E_CONFIG_INVALID, "residual window cannot be empty")
    if deltas.shape[0] != residuals.size:
        raise DriftError(
            E_CONFIG_INVALID,
            "feature delta count must equal residual count",
        )
    if not np.all(np.isfinite(residuals)) or not np.all(np.isfinite(deltas)):
        raise DriftError(E_CONFIG_INVALID, "window statistics require finite inputs")

    upstream = compute_upstream_contributions(
        deltas, coefficients, config.input_scale
    )
    scaled_residuals = residuals / config.residual_scale
    adjusted = scaled_residuals + config.upstream_weight * upstream
    weights = _decay_weights(residuals.size, config.decay)
    mean, variance, slope, trend, effective, total = _weighted_moments(
        adjusted, weights
    )
    variance = max(variance, config.variance_floor)
    standard_deviation = math.sqrt(variance)
    centered = adjusted - mean
    skewness = float(
        np.sum(weights * np.power(centered / standard_deviation, 3)) / total
    )
    upstream_contribution = float(
        np.sum(weights * np.abs(upstream)) / total
    )
    input_amplitude = float(
        np.sum(weights * np.abs(residuals)) / total
    )
    drift_score = (
        abs(mean)
        + standard_deviation
        + config.skew_weight * abs(skewness)
        + config.trend_weight * abs(trend)
        + config.upstream_weight * upstream_contribution
    )
    return WindowStatistics(
        mean=stable_float(mean),
        variance=stable_float(variance),
        standard_deviation=stable_float(standard_deviation),
        skewness=stable_float(skewness),
        trend=stable_float(trend),
        upstream_contribution=stable_float(upstream_contribution),
        input_amplitude=stable_float(input_amplitude),
        drift_score=stable_float(drift_score),
        effective_sample_count=stable_float(effective),
    )


def recompute_drift_score(
    raw_residuals: Iterable[float],
    *,
    feature_deltas: Iterable[Iterable[float]],
    coefficients: Iterable[float],
    config: StatisticsConfig,
) -> float:
    """Public independent recomputation entry point.

    No predictor history, hidden window cache, or prior score is consulted.
    """
    return compute_window_statistics(
        raw_residuals,
        feature_deltas=feature_deltas,
        coefficients=coefficients,
        config=config,
    ).drift_score


def statistics_digest(
    raw_residuals: Iterable[float],
    *,
    feature_deltas: Iterable[Iterable[float]],
    coefficients: Iterable[float],
    config: StatisticsConfig,
) -> str:
    stats = compute_window_statistics(
        raw_residuals,
        feature_deltas=feature_deltas,
        coefficients=coefficients,
        config=config,
    )
    return sha256_value(
        {
            "residuals": list(raw_residuals),
            "deltas": [list(row) for row in feature_deltas],
            "coefficients": list(coefficients),
            "config": config.to_dict(),
            "statistics": stats.to_dict(),
        }
    )

