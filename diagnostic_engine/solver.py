from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import numpy as np

from .blocking import BlockPlan
from .config import EngineConfig
from .errors import DiagnosticError
from .precision import PrecisionOps
from .utils import sha256_json


@dataclass(frozen=True)
class PathStepResult:
    path_index: int
    source_index: int
    strength: float
    coefficients: tuple[float, ...]
    intercept: float
    objective: float
    blocked_objective: float
    whole_objective: float
    objective_gap: float
    convergence_delta: float
    gradient_inf_norm: float
    iterations: int
    converged: bool
    warm_start: bool
    block_plan_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path_index": self.path_index,
            "source_index": self.source_index,
            "strength": self.strength,
            "coefficients": list(self.coefficients),
            "intercept": self.intercept,
            "objective": self.objective,
            "blocked_objective": self.blocked_objective,
            "whole_objective": self.whole_objective,
            "objective_gap": self.objective_gap,
            "convergence_delta": self.convergence_delta,
            "gradient_inf_norm": self.gradient_inf_norm,
            "iterations": self.iterations,
            "converged": self.converged,
            "warm_start": self.warm_start,
            "block_plan_sha256": self.block_plan_sha256,
        }


@dataclass
class PathRun:
    precision: str
    steps: list[PathStepResult]
    coefficients: np.ndarray
    intercept: float
    matrix: np.ndarray
    integer_matrix: np.ndarray | None
    block_plan: BlockPlan
    whole_plan: BlockPlan
    issues: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "precision": self.precision,
            "steps": [step.to_dict() for step in self.steps],
            "final_coefficients": self.coefficients.tolist(),
            "final_intercept": self.intercept,
            "block_plan": self.block_plan.to_dict(),
            "whole_plan": self.whole_plan.to_dict(),
            "feature_storage_dtype": (
                "int64" if self.integer_matrix is not None else "float"
            ),
            "integer_matrix_sha256": (
                sha256_json(self.integer_matrix.tolist())
                if self.integer_matrix is not None
                else None
            ),
            "issues": list(self.issues),
        }


def _regularization_terms(
    coefficients: np.ndarray, config: EngineConfig
) -> tuple[float, float]:
    if config.regularization == "l2":
        return config.l1_ratio * 0.0 + 1.0, 0.0
    if config.regularization == "l1":
        return 0.0, 1.0
    if config.regularization == "elastic_net":
        return 1.0 - config.l1_ratio, config.l1_ratio
    raise DiagnosticError(
        "E_CONFIG_INVALID",
        "path_solve",
        f"unsupported regularization {config.regularization!r}",
    )


def regularization_objective(
    coefficients: np.ndarray,
    strength: float,
    config: EngineConfig,
    ops: PrecisionOps,
) -> float:
    l2_ratio, l1_ratio = _regularization_terms(coefficients, config)
    l2_sum = ops.sum(np.square(coefficients))
    l1_sum = ops.sum(np.abs(coefficients))
    result = (
        0.5 * strength * l2_ratio * float(l2_sum)
        + strength * l1_ratio * float(l1_sum)
    )
    return ops.cast_scalar(result)


def _loss_for_plan(
    matrix: np.ndarray,
    labels: np.ndarray,
    coefficients: np.ndarray,
    intercept: float,
    plan: BlockPlan,
    ops: PrecisionOps,
) -> float:
    total = 0.0
    for start, stop in plan.blocks:
        block_x = matrix[start:stop]
        block_y = np.asarray(labels[start:stop], dtype=np.float64)
        logits = ops.matmul(block_x, coefficients) + intercept
        # Stable log(1 + exp(z)) - y*z.
        losses = (
            np.maximum(logits, 0.0)
            - block_y * logits
            + np.log1p(np.exp(-np.abs(logits)))
        )
        total += float(ops.sum(losses))
    return ops.cast_scalar(total / float(matrix.shape[0]))


def evaluate_objective(
    matrix: np.ndarray,
    labels: np.ndarray,
    coefficients: np.ndarray,
    intercept: float,
    strength: float,
    config: EngineConfig,
    ops: PrecisionOps,
    plan: BlockPlan,
) -> float:
    data_loss = _loss_for_plan(
        matrix, labels, coefficients, intercept, plan, ops
    )
    regularization = regularization_objective(
        coefficients, strength, config, ops
    )
    return ops.cast_scalar(data_loss + regularization)


def compute_gradient(
    matrix: np.ndarray,
    labels: np.ndarray,
    coefficients: np.ndarray,
    intercept: float,
    plan: BlockPlan,
    ops: PrecisionOps,
) -> tuple[np.ndarray, float]:
    gradient = np.zeros_like(coefficients)
    intercept_gradient = 0.0
    sample_count = float(matrix.shape[0])
    for start, stop in plan.blocks:
        block_x = matrix[start:stop]
        block_y = np.asarray(labels[start:stop], dtype=np.float64)
        logits = ops.matmul(block_x, coefficients) + intercept
        probabilities = ops.sigmoid(logits)
        residual = ops.quantize(probabilities - block_y)
        block_gradient = ops.matmul(block_x.T, residual)
        gradient = ops.quantize(gradient + block_gradient)
        intercept_gradient = ops.cast_scalar(
            intercept_gradient + float(ops.sum(residual))
        )
    gradient = ops.quantize(gradient / sample_count)
    intercept_gradient = ops.cast_scalar(intercept_gradient / sample_count)
    return gradient, intercept_gradient


def _proximal_update(
    coefficients: np.ndarray,
    gradient: np.ndarray,
    intercept: float,
    intercept_gradient: float,
    strength: float,
    config: EngineConfig,
    ops: PrecisionOps,
) -> tuple[np.ndarray, float]:
    learning_rate = ops.cast_scalar(config.learning_rate)
    l2_ratio, l1_ratio = _regularization_terms(coefficients, config)
    stepped = ops.quantize(
        coefficients - learning_rate * ops.quantize(gradient)
    )
    l2_penalty = learning_rate * strength * l2_ratio
    if l2_penalty > 0.0:
        stepped = ops.quantize(stepped / (1.0 + l2_penalty))
    threshold = learning_rate * strength * l1_ratio
    if threshold > 0.0:
        magnitude = np.maximum(np.abs(stepped) - threshold, 0.0)
        stepped = ops.quantize(np.sign(stepped) * magnitude)
    new_intercept = ops.cast_scalar(
        intercept - learning_rate * intercept_gradient
    )
    return stepped, new_intercept


def _solve_strength(
    *,
    matrix: np.ndarray,
    labels: np.ndarray,
    initial_coefficients: np.ndarray,
    initial_intercept: float,
    strength: float,
    config: EngineConfig,
    ops: PrecisionOps,
    plan: BlockPlan,
    warm_start: bool,
    iteration_limit: int,
    canonical_converged: bool | None,
) -> tuple[np.ndarray, float, int, bool, float, float]:
    coefficients = ops.quantize(initial_coefficients.copy())
    intercept = ops.cast_scalar(initial_intercept)
    tolerance = ops.cast_scalar(config.tolerance)
    converged = False
    delta = float("inf")
    iterations = 0
    for iteration in range(1, iteration_limit + 1):
        gradient, intercept_gradient = compute_gradient(
            matrix, labels, coefficients, intercept, plan, ops
        )
        next_coefficients, next_intercept = _proximal_update(
            coefficients,
            gradient,
            intercept,
            intercept_gradient,
            strength,
            config,
            ops,
        )
        delta = max(
            float(np.max(np.abs(next_coefficients - coefficients)))
            if coefficients.size
            else 0.0,
            abs(next_intercept - intercept),
        )
        coefficients = next_coefficients
        intercept = next_intercept
        iterations = iteration
        if canonical_converged is None and delta <= tolerance:
            converged = True
            break
    if canonical_converged is not None:
        converged = canonical_converged
    if not np.isfinite(coefficients).all() or not np.isfinite(intercept):
        raise DiagnosticError(
            "E_NUMERIC_NONFINITE",
            "path_solve",
            "solver produced non-finite parameters",
        )
    gradient, _ = compute_gradient(
        matrix, labels, coefficients, intercept, plan, ops
    )
    gradient_inf = (
        float(np.max(np.abs(gradient))) if gradient.size else 0.0
    )
    return (
        coefficients,
        intercept,
        iterations,
        converged,
        delta,
        gradient_inf,
    )


def fit_regularization_path(
    *,
    matrix: np.ndarray,
    labels: np.ndarray,
    strengths: list[float],
    config: EngineConfig,
    ops: PrecisionOps,
    block_plan: BlockPlan,
    whole_plan: BlockPlan,
    integer_matrix: np.ndarray | None = None,
    start_index: int = 0,
    completed_steps: list[PathStepResult] | None = None,
    on_step: Callable[[int, PathStepResult], None] | None = None,
    convergence_schedule: Sequence[tuple[int, bool]] | None = None,
) -> PathRun:
    if not strengths:
        raise DiagnosticError(
            "E_PATH_EMPTY", "path_solve", "regularization path is empty"
        )
    ordered = sorted(
        enumerate(strengths), key=lambda item: (-item[1], item[0])
    )
    if convergence_schedule is not None and len(convergence_schedule) != len(ordered):
        raise DiagnosticError(
            "E_CONVERGENCE_SCHEDULE",
            "path_solve",
            "canonical convergence schedule length does not match path",
        )
    completed = list(completed_steps or [])
    if completed:
        if len(completed) != start_index:
            raise DiagnosticError(
                "E_CHECKPOINT_PATH_MISMATCH",
                "path_solve",
                "completed steps do not align with start_index",
            )
        coefficients = np.asarray(
            completed[-1].coefficients, dtype=np.float64
        )
        intercept = float(completed[-1].intercept)
    else:
        coefficients = np.zeros(matrix.shape[1], dtype=np.float64)
        intercept = 0.0
    steps = completed
    for path_index in range(start_index, len(ordered)):
        source_index, strength = ordered[path_index]
        warm_start = coefficients.any() or intercept != 0.0
        if convergence_schedule is None:
            iteration_limit = config.max_iter
            canonical_converged = None
        else:
            iteration_limit, canonical_converged = convergence_schedule[path_index]
            if not 1 <= int(iteration_limit) <= config.max_iter:
                raise DiagnosticError(
                    "E_CONVERGENCE_SCHEDULE",
                    "path_solve",
                    "canonical iteration limit is outside configured bounds",
                )
            iteration_limit = int(iteration_limit)
        (
            coefficients,
            intercept,
            iterations,
            converged,
            delta,
            gradient_inf,
        ) = _solve_strength(
            matrix=matrix,
            labels=labels,
            initial_coefficients=coefficients,
            initial_intercept=intercept,
            strength=strength,
            config=config,
            ops=ops,
            plan=block_plan,
            warm_start=warm_start,
            iteration_limit=iteration_limit,
            canonical_converged=canonical_converged,
        )
        blocked_objective = evaluate_objective(
            matrix,
            labels,
            coefficients,
            intercept,
            strength,
            config,
            ops,
            block_plan,
        )
        whole_objective = evaluate_objective(
            matrix,
            labels,
            coefficients,
            intercept,
            strength,
            config,
            ops,
            whole_plan,
        )
        step = PathStepResult(
            path_index=path_index,
            source_index=source_index,
            strength=float(strength),
            coefficients=tuple(float(item) for item in coefficients),
            intercept=float(intercept),
            objective=float(blocked_objective),
            blocked_objective=float(blocked_objective),
            whole_objective=float(whole_objective),
            objective_gap=float(abs(blocked_objective - whole_objective)),
            convergence_delta=float(delta),
            gradient_inf_norm=float(gradient_inf),
            iterations=iterations,
            converged=converged,
            warm_start=warm_start,
            block_plan_sha256=block_plan.decision_sha256,
        )
        steps.append(step)
        if on_step is not None:
            on_step(path_index, step)
    return PathRun(
        precision=ops.name,
        steps=steps,
        coefficients=ops.quantize(coefficients),
        intercept=ops.cast_scalar(intercept),
        matrix=matrix,
        integer_matrix=integer_matrix,
        block_plan=block_plan,
        whole_plan=whole_plan,
    )

