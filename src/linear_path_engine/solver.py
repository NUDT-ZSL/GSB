"""Deterministic proximal-gradient path solver over block-reduced gradients."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from .config import RunConfig
from .errors import E_RESOURCE_LIMIT, E_STATE, EngineError
from .objective import penalty_terms
from .parallel import BlockExecutor
from .planner import ChunkPlan
from .precision import PrecisionOps


@dataclass
class ComparisonBudget:
    limit: int
    used: int = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def consume(
        self,
        amount: int,
        *,
        strength_index: int | None = None,
        strength: float | None = None,
        stage: str = "PATH_SOLVE",
    ) -> None:
        if amount < 0:
            raise EngineError(E_STATE, "comparison count cannot be negative", stage=stage)
        if self.used + amount > self.limit:
            raise EngineError(
                E_RESOURCE_LIMIT,
                "comparison budget exhausted",
                stage=stage,
                strength=strength,
                strength_index=strength_index,
                details={
                    "used": self.used,
                    "requested": amount,
                    "limit": self.limit,
                },
            )
        self.used += amount


class PathSolver:
    def __init__(
        self,
        *,
        config: RunConfig,
        standardized_matrix: np.ndarray,
        targets: np.ndarray,
        plan: ChunkPlan,
        budget: ComparisonBudget,
        stats_version: str,
        fault_spec: str | None = None,
        iteration_hook: Callable[[int, int], None] | None = None,
    ) -> None:
        self.config = config
        self.ops = PrecisionOps.from_config(config.precision)
        self.encoded_matrix = self.ops.encode_matrix(standardized_matrix)
        self.encoded_targets = self.ops.encode_targets(targets)
        self.targets = targets
        self.plan = plan
        self.budget = budget
        self.stats_version = stats_version
        self.iteration_hook = iteration_hook
        self.lipschitz, self.step_size = self.ops.lipschitz_and_step(
            self.encoded_matrix, config.solver.lipschitz_safety
        )
        self._executor = BlockExecutor(
            matrix=self.encoded_matrix,
            targets=self.encoded_targets,
            precision=config.precision,
            plan=plan,
            fault_spec=fault_spec,
        )

    def __enter__(self) -> "PathSolver":
        self._executor.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._executor.__exit__(exc_type, exc, traceback)

    def _reduce_gradients(
        self, results: list[dict[str, Any]]
    ) -> tuple[np.ndarray, Any, float, int]:
        gradient = np.zeros_like(self.ops.zero_params(self.encoded_matrix.shape[1])[0])
        intercept_gradient = self.ops.zero_params(self.encoded_matrix.shape[1])[1]
        sse = 0.0
        comparisons = 0
        for block_index, result in enumerate(results):
            if result["block_index"] != block_index:
                raise EngineError(
                    E_STATE,
                    "block results are not in reduction order",
                    stage="PATH_SOLVE",
                    details={
                        "expected_block_index": block_index,
                        "actual_block_index": result["block_index"],
                    },
                )
            gradient = gradient + np.asarray(result["gradient"], dtype=gradient.dtype)
            intercept_gradient = self.ops.dtype.type(
                intercept_gradient + result["intercept_gradient"]
            )
            sse += float(result["sse"])
            comparisons += int(result["comparisons"])
        return gradient, intercept_gradient, sse, comparisons

    def _reduce_sse(self, results: list[dict[str, Any]]) -> tuple[float, float, int]:
        sse = 0.0
        absolute_sum = 0.0
        comparisons = 0
        for result in results:
            sse += float(result["sse"])
            absolute_sum += float(result["abs_residual_sum"])
            comparisons += int(result["comparisons"])
        return sse, absolute_sum, comparisons

    def _gradient_inf_norm(self, gradient: np.ndarray, intercept_gradient: Any) -> float:
        if self.ops.mode == "fixed_point":
            scale = float(self.ops.fixed_scale)
            return float(
                max(
                    np.max(np.abs(gradient.astype(np.float64))) / scale,
                    abs(float(intercept_gradient)) / scale,
                )
            )
        return float(
            max(
                np.max(np.abs(np.asarray(gradient, dtype=np.float64))),
                abs(float(intercept_gradient)),
            )
        )

    def solve_strength(
        self,
        *,
        ascending_index: int,
        solve_position: int,
        initial_weights: np.ndarray,
        initial_intercept: float,
        warm_start_from_strength: float | None,
    ) -> dict[str, Any]:
        strength = self.config.model.strengths_ascending[ascending_index]
        max_iterations = self.config.solver.iterations_for(ascending_index)
        tolerance = self.config.solver.tolerance_for(ascending_index)
        weights, intercept = self.ops.encode_params(initial_weights, initial_intercept)
        previous_weights = weights
        previous_intercept = intercept
        extrapolated_weights = weights
        extrapolated_intercept = intercept
        momentum_t = 1.0
        comparisons_start = self.budget.used
        converged = False
        termination_reason = "iteration_limit"
        iterations = 0
        last_gradient_inf = float("inf")

        for iteration in range(1, max_iterations + 1):
            results = self._executor.run(
                weights=extrapolated_weights,
                intercept=extrapolated_intercept,
                strength_index=ascending_index,
                strength=strength,
                operation="gradient",
            )
            gradient, intercept_gradient, sse, comparisons = self._reduce_gradients(results)
            self.budget.consume(
                comparisons,
                strength_index=ascending_index,
                strength=strength,
            )
            last_gradient_inf = self._gradient_inf_norm(gradient, intercept_gradient)
            new_weights, new_intercept = self.ops.apply_proximal_step(
                extrapolated_weights,
                extrapolated_intercept,
                gradient,
                intercept_gradient,
                step_size=self.step_size,
                strength=strength,
                l1_ratio=self.config.model.l1_ratio,
            )
            delta = self.ops.parameter_inf_norm_change(
                previous_weights, previous_intercept, new_weights, new_intercept
            )
            restart_measure = self.ops.restart_measure(
                extrapolated_weights,
                extrapolated_intercept,
                new_weights,
                new_intercept,
                previous_weights,
                previous_intercept,
            )
            self.budget.consume(
                self.encoded_matrix.shape[1] + 1,
                strength_index=ascending_index,
                strength=strength,
            )
            old_weights, old_intercept = previous_weights, previous_intercept
            previous_weights, previous_intercept = new_weights, new_intercept
            if restart_measure > 0.0:
                momentum_t = 1.0
                extrapolated_weights, extrapolated_intercept = new_weights, new_intercept
            else:
                next_t = 0.5 * (
                    1.0 + float(np.sqrt(1.0 + 4.0 * momentum_t * momentum_t))
                )
                momentum = (momentum_t - 1.0) / next_t
                extrapolated_weights, extrapolated_intercept = self.ops.extrapolate(
                    old_weights,
                    old_intercept,
                    new_weights,
                    new_intercept,
                    momentum,
                )
                momentum_t = next_t
            weights, intercept = new_weights, new_intercept
            iterations = iteration
            if self.iteration_hook is not None:
                self.iteration_hook(ascending_index, iteration)
            if delta <= tolerance:
                converged = True
                termination_reason = "parameter_tolerance"
                break

        final_results = self._executor.run(
            weights=weights,
            intercept=intercept,
            strength_index=ascending_index,
            strength=strength,
            operation="gradient",
        )
        final_gradient, final_intercept_gradient, final_sse, final_comparisons = (
            self._reduce_gradients(final_results)
        )
        self.budget.consume(
            final_comparisons,
            strength_index=ascending_index,
            strength=strength,
        )
        final_gradient_inf = self._gradient_inf_norm(
            final_gradient, final_intercept_gradient
        )
        decoded_weights, decoded_intercept = self.ops.decode_params(weights, intercept)
        penalty_l1, penalty_l2, penalty = penalty_terms(
            decoded_weights, strength, self.config.model.l1_ratio
        )
        n_samples = self.encoded_matrix.shape[0]
        squared_loss = 0.5 * final_sse / float(n_samples)
        objective = float(squared_loss + penalty)
        coefficients = [float(value) for value in decoded_weights.tolist()]
        l2_norm = float(np.linalg.norm(decoded_weights, ord=2))
        l1_norm = float(np.linalg.norm(decoded_weights, ord=1))
        return {
            "strength": float(strength),
            "strength_index": int(ascending_index),
            "solve_position": int(solve_position),
            "coefficients": coefficients,
            "intercept": float(decoded_intercept),
            "objective": objective,
            "objective_components": {
                "squared_loss": float(squared_loss),
                "sse": float(final_sse),
                "penalty": float(penalty),
                "penalty_l1": float(penalty_l1),
                "penalty_l2": float(penalty_l2),
            },
            "converged": bool(converged),
            "status": "converged" if converged else "max_iterations",
            "termination_reason": termination_reason,
            "iterations": int(iterations),
            "tolerance": float(tolerance),
            "max_iterations": int(max_iterations),
            "gradient_inf_norm": float(last_gradient_inf),
            "final_gradient_inf_norm": float(final_gradient_inf),
            "coefficient_norm_l2": l2_norm,
            "coefficient_norm_l1": l1_norm,
            "stats_version": self.stats_version,
            "precision_mode": self.config.precision.mode,
            "warm_start_from_strength": (
                float(warm_start_from_strength)
                if warm_start_from_strength is not None
                else None
            ),
            "comparisons": int(self.budget.used - comparisons_start),
            "lipschitz": float(self.lipschitz),
            "step_size": float(self.step_size),
        }

