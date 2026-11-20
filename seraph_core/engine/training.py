"""Chunked linear-regression training core.

The same routine serves both reduction modes:

* ``chunk_size == 0`` (or >= n rows) produces the one-shot whole-batch path;
* otherwise rows are split into deterministic contiguous chunks whose partial
  gradients are reduced strictly left-to-right (``reduction_order =
  "sequential"``).

All arithmetic goes through a :class:`PrecisionBackend`, so float64, float32
and fixed-point runs execute identical operator sequences and differ only by
rounding.  Loss is always re-evaluated over the complete dataset with the same
evaluation routine, keeping loss trajectories a pure function of the weights.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..errors import E_RESOURCE_LIMIT, EngineError
from .precision import PrecisionBackend, make_backend


def chunk_plan(n_rows: int, chunk_size: int) -> list[tuple[int, int]]:
    """Deterministic contiguous chunk boundaries [start, end)."""
    if chunk_size <= 0 or chunk_size >= n_rows:
        return [(0, n_rows)] if n_rows else []
    return [(start, min(start + chunk_size, n_rows))
            for start in range(0, n_rows, chunk_size)]


def augment(matrix: np.ndarray) -> np.ndarray:
    bias = np.ones((matrix.shape[0], 1), dtype=matrix.dtype)
    return np.concatenate([matrix, bias], axis=1)


@dataclass
class EpochRecord:
    epoch: int
    loss: float
    improvement: float
    converged: bool
    convergence_reason: str

    def to_dict(self) -> dict:
        return {
            "epoch": self.epoch,
            "loss": self.loss,
            "improvement": self.improvement,
            "converged": self.converged,
            "convergence_reason": self.convergence_reason,
        }


class LinearRegressor:
    """Two-target (valence, arousal) linear model with bias column."""

    def __init__(self, n_features: int, config):
        self.config = config
        self.backend: PrecisionBackend = make_backend(config)
        self.n_features = n_features
        # Deterministic zero initialisation; bias is the last row.
        self.weights = np.zeros((n_features + 1, 2), dtype=np.float64)

    # ------------------------------------------------------------------ #
    # Forward / loss
    # ------------------------------------------------------------------ #
    def predict(self, x_aug: np.ndarray) -> np.ndarray:
        columns = [self.backend.matvec(x_aug, self.weights[:, target])
                   for target in range(2)]
        return np.stack(columns, axis=1)

    def loss_mse(self, x_aug: np.ndarray, y: np.ndarray) -> float:
        prediction = self.predict(x_aug)
        residual = prediction - y
        n_rows = x_aug.shape[0]
        per_row = [self.backend.dot(residual[i], residual[i])
                   for i in range(n_rows)]
        total = self.backend.sequential_sum(per_row)
        return total / (2.0 * n_rows)

    # ------------------------------------------------------------------ #
    # Gradient with explicit chunk reduction
    # ------------------------------------------------------------------ #
    def gradient(self, x_aug: np.ndarray, y: np.ndarray,
                 plan: list[tuple[int, int]]) -> np.ndarray:
        n_rows = x_aug.shape[0]
        partial_grads: list[np.ndarray] = []
        for start, end in plan:
            x_chunk = x_aug[start:end]
            pred = self.predict(x_chunk)
            residual = pred - y[start:end]
            grad = np.zeros_like(self.weights)
            for feature in range(x_aug.shape[1]):
                for target in range(2):
                    grad[feature, target] = self.backend.dot(
                        x_chunk[:, feature], residual[:, target])
            partial_grads.append(grad)
        # Sequential left-to-right reduction of chunk partials.
        grad = np.zeros_like(self.weights)
        for partial in partial_grads:
            grad = self.backend.add(grad, partial)
        grad = grad / n_rows
        if self.config.regularization > 0.0:
            penalty = self.weights.copy()
            penalty[-1, :] = 0.0  # bias is not regularised
            grad = grad + self.config.regularization * penalty
        return grad

    def apply_update(self, grad: np.ndarray) -> None:
        step = self.backend.scale(self.config.learning_rate,
                                  self.config.lr_scale)
        for feature in range(self.weights.shape[0]):
            for target in range(self.weights.shape[1]):
                delta = self.backend.scale(grad[feature, target], step)
                self.weights[feature, target] = self.backend.add(
                    np.array([self.weights[feature, target]]),
                    np.array([-delta]))[0]

    # ------------------------------------------------------------------ #
    # Convergence gates (requirement 6)
    # ------------------------------------------------------------------ #
    def check_convergence(self, previous_loss: float,
                          current_loss: float) -> tuple[bool, float, str]:
        improvement = previous_loss - current_loss
        if self.config.subnormal_protection:
            floor = np.finfo(np.float64).tiny * 8.0
            if abs(improvement) < floor:
                improvement = 0.0
        tol = self.config.convergence_tolerance
        if self.config.convergence_strategy == "strict":
            if improvement < tol:
                return True, improvement, "strict:improvement<tolerance"
            return False, improvement, "running"
        band = self.config.tolerance_band
        if improvement < tol + band:
            return True, improvement, "tolerance_band:improvement<tolerance+band"
        if self.config.min_improvement > 0.0 and \
                improvement < self.config.min_improvement:
            return True, improvement, "min_improvement:below_floor"
        return False, improvement, "running"

    # ------------------------------------------------------------------ #
    # Full epoch loop (resumable via ``start_epoch`` / injected weights)
    # ------------------------------------------------------------------ #
    def train(self, x_aug: np.ndarray, y: np.ndarray,
              plan: list[tuple[int, int]], start_epoch: int = 1,
              previous_loss: float | None = None
              ) -> tuple[list[EpochRecord], int]:
        history: list[EpochRecord] = []
        converged_epoch = 0
        prev_loss = previous_loss if previous_loss is not None \
            else self.loss_mse(x_aug, y)
        for epoch in range(start_epoch, self.config.epochs + 1):
            grad = self.gradient(x_aug, y, plan)
            self.apply_update(grad)
            current_loss = self.loss_mse(x_aug, y)
            converged, improvement, reason = self.check_convergence(
                prev_loss, current_loss)
            record = EpochRecord(epoch, current_loss, improvement,
                                 converged, reason)
            history.append(record)
            prev_loss = current_loss
            if converged:
                converged_epoch = epoch
                break
        return history, converged_epoch


def recompute_loss(weights: np.ndarray, x_aug: np.ndarray, y: np.ndarray,
                   config) -> float:
    """Independent loss recomputation used by cross-module self-checks."""
    model = LinearRegressor(x_aug.shape[1] - 1, config)
    model.weights = weights.astype(np.float64).copy()
    return model.loss_mse(x_aug, y)


def estimate_gradient_bytes(n_features: int, n_rows: int, chunk_size: int,
                            precision: str) -> int:
    item_bytes = 8 if precision == "float64" else (
        4 if precision == "float32" else 8)
    effective_rows = n_rows if chunk_size <= 0 else min(chunk_size, n_rows)
    # Residual + chunk design block + gradient partials.
    return item_bytes * effective_rows * (n_features * 2 + 3)


def enforce_resource_limits(n_dialogs: int, n_segments: int, n_features: int,
                            config) -> tuple[int, list[dict]]:
    """Validate caps and degrade chunk size when the gradient budget is hit."""
    limits = config.resource_limits
    degradations: list[dict] = []
    if n_dialogs > limits.max_dialogs:
        raise EngineError(E_RESOURCE_LIMIT,
                          f"dialog count {n_dialogs} exceeds limit "
                          f"{limits.max_dialogs}", field="max_dialogs")
    if n_segments > limits.max_resident_segments:
        raise EngineError(E_RESOURCE_LIMIT,
                          f"resident segments {n_segments} exceed limit "
                          f"{limits.max_resident_segments}",
                          field="max_resident_segments")
    chunk_size = config.chunk_size
    estimated = estimate_gradient_bytes(
        n_features, n_segments, chunk_size, config.precision)
    if estimated > limits.max_gradient_bytes and chunk_size > 0:
        floor = limits.min_chunk_size
        while chunk_size > floor:
            chunk_size = max(floor, chunk_size // 2)
            estimated = estimate_gradient_bytes(
                n_features, n_segments, chunk_size, config.precision)
            if estimated <= limits.max_gradient_bytes:
                break
        degradations.append({
            "category": "chunk_size_downgrade",
            "original_chunk_size": config.chunk_size,
            "effective_chunk_size": chunk_size,
            "estimated_bytes": estimated,
            "limit_bytes": limits.max_gradient_bytes,
        })
    if estimated > limits.max_gradient_bytes:
        raise EngineError(E_RESOURCE_LIMIT,
                          f"gradient memory estimate {estimated} bytes exceeds "
                          f"limit {limits.max_gradient_bytes} even at minimum "
                          f"chunk size", field="max_gradient_bytes")
    return chunk_size, degradations
