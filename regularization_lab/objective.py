"""Explicit L2, L1 and elastic-net objectives and deterministic gradients."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import RegularizerSpec


@dataclass(frozen=True)
class RegularizationEvaluation:
    loss: float
    gradient: np.ndarray
    l1_loss: float
    l2_loss: float
    l1_gradient: np.ndarray
    l2_gradient: np.ndarray

    def components(self) -> dict[str, float]:
        return {"l1": self.l1_loss, "l2": self.l2_loss}


def l1_subgradient(values: np.ndarray) -> np.ndarray:
    """Fixed subgradient convention: sign(0) = 0."""

    return np.where(values > 0.0, 1.0, np.where(values < 0.0, -1.0, 0.0))


def evaluate_regularization(weights: np.ndarray, scheme: RegularizerSpec) -> RegularizationEvaluation:
    weights = np.asarray(weights, dtype=np.float64)
    absolute_sum = float(np.sum(np.abs(weights)))
    squared_sum = float(np.sum(weights * weights))

    if scheme.kind == "l2":
        l1_loss = 0.0
        l2_loss = 0.5 * scheme.alpha * squared_sum
        l1_gradient = np.zeros_like(weights)
        l2_gradient = scheme.alpha * weights
    elif scheme.kind == "l1":
        l1_loss = scheme.alpha * absolute_sum
        l2_loss = 0.0
        l1_gradient = scheme.alpha * l1_subgradient(weights)
        l2_gradient = np.zeros_like(weights)
    elif scheme.kind == "elastic_net":
        l1_weight = scheme.alpha * scheme.l1_ratio
        l2_weight = scheme.alpha * (1.0 - scheme.l1_ratio)
        l1_loss = l1_weight * absolute_sum
        l2_loss = 0.5 * l2_weight * squared_sum
        l1_gradient = l1_weight * l1_subgradient(weights)
        l2_gradient = l2_weight * weights
    else:  # Defensive: configuration validation rejects unknown kinds.
        raise ValueError(f"unsupported regularization type {scheme.kind!r}")

    return RegularizationEvaluation(
        loss=float(l1_loss + l2_loss),
        gradient=np.asarray(l1_gradient + l2_gradient, dtype=np.float64),
        l1_loss=float(l1_loss),
        l2_loss=float(l2_loss),
        l1_gradient=np.asarray(l1_gradient, dtype=np.float64),
        l2_gradient=np.asarray(l2_gradient, dtype=np.float64),
    )


def mean_squared_error(predictions: np.ndarray, targets: np.ndarray) -> float:
    predictions = np.asarray(predictions, dtype=np.float64)
    targets = np.asarray(targets, dtype=np.float64)
    residual = predictions - targets
    return float(np.sum(residual * residual) / residual.size)
