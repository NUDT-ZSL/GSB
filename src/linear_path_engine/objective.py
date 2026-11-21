"""Independent objective recomputation in float64."""

from __future__ import annotations

from typing import Any

import numpy as np

from .config import ModelConfig


def penalty_terms(
    weights: np.ndarray, strength: float, l1_ratio: float
) -> tuple[float, float, float]:
    l1 = float(strength * l1_ratio * np.sum(np.abs(weights), dtype=np.float64))
    l2 = float(
        0.5
        * strength
        * (1.0 - l1_ratio)
        * np.dot(weights.astype(np.float64), weights.astype(np.float64))
    )
    return l1, l2, l1 + l2


def objective_terms(
    matrix: np.ndarray,
    targets: np.ndarray,
    weights: np.ndarray,
    intercept: float,
    strength: float,
    l1_ratio: float,
) -> dict[str, Any]:
    weights64 = np.asarray(weights, dtype=np.float64)
    predictions = matrix.astype(np.float64, copy=False) @ weights64 + float(intercept)
    residuals = predictions - targets.astype(np.float64, copy=False)
    n_samples = matrix.shape[0]
    sse = float(np.dot(residuals, residuals))
    mse = sse / float(n_samples)
    squared_loss = 0.5 * mse
    l1, l2, penalty = penalty_terms(weights64, strength, l1_ratio)
    objective = squared_loss + penalty
    return {
        "squared_loss": float(squared_loss),
        "sse": sse,
        "mse": float(mse),
        "penalty": float(penalty),
        "penalty_l1": float(l1),
        "penalty_l2": float(l2),
        "objective": float(objective),
    }


def model_metadata(model: ModelConfig) -> dict[str, Any]:
    return {
        "loss": model.loss,
        "regularization": model.regularization,
        "l1_ratio": model.l1_ratio,
    }

