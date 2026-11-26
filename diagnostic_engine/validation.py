from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from .blocking import BlockPlan
from .config import EngineConfig
from .data import Dataset, PreparedFeatures
from .errors import DiagnosticError
from .precision import PrecisionOps
from .solver import PathRun, evaluate_objective


def _divergence(
    *,
    check: str,
    field: str,
    expected: float,
    actual: float,
    tolerance: float,
    **context: Any,
) -> dict[str, Any]:
    return {
        "check": check,
        "field": field,
        "expected": expected,
        "actual": actual,
        "absolute_delta": abs(actual - expected),
        "tolerance": tolerance,
        **context,
    }


def _check_block_coverage(plan: BlockPlan, sample_count: int) -> list[str]:
    errors: list[str] = []
    expected_start = 0
    for start, stop in plan.blocks:
        if start != expected_start:
            errors.append(f"gap_or_overlap_at={start}")
        if stop <= start:
            errors.append(f"empty_block_at={start}")
        expected_start = stop
    if expected_start != sample_count:
        errors.append(f"last_end={expected_start}")
    return errors


def _recompute_numeric_matrix(
    dataset: Dataset,
    prepared: PreparedFeatures,
    config: EngineConfig,
) -> np.ndarray:
    numeric_columns = len(config.numeric_columns)
    raw = dataset.numeric
    if config.standardization == "zscore":
        recomputed = (
            raw - np.asarray(prepared.stats.mean, dtype=np.float64)
        ) / np.asarray(prepared.stats.scale, dtype=np.float64)
    else:
        recomputed = (
            raw - np.asarray(prepared.stats.minimum, dtype=np.float64)
        ) / np.asarray(prepared.stats.scale, dtype=np.float64)
    return recomputed[:, :numeric_columns]


def validate_results(
    *,
    config: EngineConfig,
    dataset: Dataset,
    prepared: PreparedFeatures,
    labels: np.ndarray,
    runs: dict[str, PathRun],
    reference: PathRun,
    block_plan: BlockPlan,
    injection_issues: Iterable[dict[str, Any]],
    tolerance: float,
) -> dict[str, Any]:
    sample_count = prepared.matrix.shape[0]
    checks: list[dict[str, Any]] = []
    divergences: list[dict[str, Any]] = []
    coverage_errors = _check_block_coverage(block_plan, sample_count)
    checks.append(
        {
            "check": "block_coverage",
            "passed": not coverage_errors,
            "errors": coverage_errors,
        }
    )
    if coverage_errors:
        divergences.append(
            {
                "check": "block_coverage",
                "field": "blocks",
                "errors": coverage_errors,
            }
        )
    recomputed_numeric = _recompute_numeric_matrix(dataset, prepared, config)
    numeric_columns = recomputed_numeric.shape[1]
    numeric_delta = (
        float(
            np.max(
                np.abs(
                    recomputed_numeric
                    - prepared.matrix[:, :numeric_columns]
                )
            )
        )
        if recomputed_numeric.size
        else 0.0
    )
    numeric_ok = numeric_delta <= max(tolerance, 1e-12)
    checks.append(
        {
            "check": "standardization_recompute",
            "passed": numeric_ok,
            "max_abs_delta": numeric_delta,
            "tolerance": max(tolerance, 1e-12),
        }
    )
    if not numeric_ok:
        divergences.append(
            _divergence(
                check="standardization_recompute",
                field="numeric_matrix",
                expected=0.0,
                actual=numeric_delta,
                tolerance=max(tolerance, 1e-12),
            )
        )
    for precision_name, run in runs.items():
        ops = PrecisionOps.from_config(config, precision_name)
        if run.block_plan.decision_sha256 != block_plan.decision_sha256:
            divergences.append(
                {
                    "check": "shared_block_plan",
                    "precision": precision_name,
                    "field": "block_plan_sha256",
                    "expected": block_plan.decision_sha256,
                    "actual": run.block_plan.decision_sha256,
                }
            )
        for step in run.steps:
            if not np.isfinite(step.objective):
                divergences.append(
                    {
                        "check": "objective_finite",
                        "precision": precision_name,
                        "path_index": step.path_index,
                        "field": "objective",
                        "actual": step.objective,
                    }
                )
            if step.strength <= 0.0:
                divergences.append(
                    {
                        "check": "positive_strength",
                        "precision": precision_name,
                        "path_index": step.path_index,
                        "field": "strength",
                        "actual": step.strength,
                    }
                )
            recomputed_objective = evaluate_objective(
                run.matrix,
                labels,
                np.asarray(step.coefficients, dtype=np.float64),
                step.intercept,
                step.strength,
                config,
                ops,
                run.block_plan,
            )
            objective_delta = abs(recomputed_objective - step.objective)
            objective_tol = max(
                config.objective_tolerance, tolerance, ops.quantum * 16.0
            )
            if objective_delta > objective_tol:
                divergences.append(
                    _divergence(
                        check="objective_recompute",
                        field="objective",
                        expected=recomputed_objective,
                        actual=step.objective,
                        tolerance=objective_tol,
                        precision=precision_name,
                        path_index=step.path_index,
                    )
                )
            gap_tol = max(
                config.objective_tolerance, tolerance, ops.quantum * 16.0
            )
            if step.objective_gap > gap_tol:
                divergences.append(
                    _divergence(
                        check="block_vs_whole_objective",
                        field="objective_gap",
                        expected=0.0,
                        actual=step.objective_gap,
                        tolerance=gap_tol,
                        precision=precision_name,
                        path_index=step.path_index,
                    )
                )
            convergence_floor = max(
                config.tolerance, ops.quantum * 2.0
            )
            if step.converged and step.convergence_delta > convergence_floor:
                divergences.append(
                    {
                        "check": "convergence_marker",
                        "precision": precision_name,
                        "path_index": step.path_index,
                        "field": "converged",
                        "expected": True,
                        "actual": False,
                        "convergence_delta": step.convergence_delta,
                        "tolerance_floor": convergence_floor,
                    }
                )
            if not step.converged and step.iterations < config.max_iter:
                divergences.append(
                    {
                        "check": "convergence_marker",
                        "precision": precision_name,
                        "path_index": step.path_index,
                        "field": "iterations",
                        "expected": config.max_iter,
                        "actual": step.iterations,
                        "converged": False,
                    }
                )
        if len(run.steps) != len(reference.steps):
            divergences.append(
                {
                    "check": "path_length",
                    "precision": precision_name,
                    "field": "step_count",
                    "expected": len(reference.steps),
                    "actual": len(run.steps),
                }
            )
            continue
        for observed_step, reference_step in zip(run.steps, reference.steps):
            if observed_step.converged != reference_step.converged:
                divergences.append(
                    {
                        "check": "precision_convergence_consistency",
                        "precision": precision_name,
                        "path_index": observed_step.path_index,
                        "field": "converged",
                        "expected": reference_step.converged,
                        "actual": observed_step.converged,
                    }
                )
            if observed_step.iterations != reference_step.iterations:
                divergences.append(
                    {
                        "check": "precision_iteration_schedule",
                        "precision": precision_name,
                        "path_index": observed_step.path_index,
                        "field": "iterations",
                        "expected": reference_step.iterations,
                        "actual": observed_step.iterations,
                    }
                )
            coefficient_index = -1
            coefficient_delta = 0.0
            if observed_step.coefficients:
                coefficient_differences = np.abs(
                    np.asarray(observed_step.coefficients)
                    - np.asarray(reference_step.coefficients)
                )
                coefficient_index = int(
                    np.unravel_index(
                        np.argmax(coefficient_differences),
                        coefficient_differences.shape,
                    )[0]
                )
                coefficient_delta = float(
                    coefficient_differences[coefficient_index]
                )
            coefficient_tol = max(
                config.coefficient_tolerance, tolerance, ops.quantum * 32.0
            )
            if coefficient_delta > coefficient_tol:
                divergences.append(
                    _divergence(
                        check="precision_coefficient_consistency",
                        field="coefficients",
                        expected=0.0,
                        actual=coefficient_delta,
                        tolerance=coefficient_tol,
                        precision=precision_name,
                        path_index=observed_step.path_index,
                        coefficient_index=coefficient_index,
                    )
                )
            objective_delta = abs(
                observed_step.objective - reference_step.objective
            )
            objective_tol = max(
                config.objective_tolerance, tolerance, ops.quantum * 16.0
            )
            if objective_delta > objective_tol:
                divergences.append(
                    _divergence(
                        check="precision_objective_consistency",
                        field="objective",
                        expected=reference_step.objective,
                        actual=observed_step.objective,
                        tolerance=objective_tol,
                        precision=precision_name,
                        path_index=observed_step.path_index,
                    )
                )
            checks.append(
                {
                    "check": "precision_path_consistency",
                    "precision": precision_name,
                    "path_index": observed_step.path_index,
                    "passed": (
                        coefficient_delta <= coefficient_tol
                        and objective_delta <= objective_tol
                    ),
                    "coefficient_max_abs_delta": coefficient_delta,
                    "coefficient_tolerance": coefficient_tol,
                    "objective_abs_delta": objective_delta,
                    "objective_tolerance": objective_tol,
                }
            )
    issues = list(injection_issues)
    degradation_categories = sorted(
        {
            issue["degrade_category"]
            for issue in issues
            if issue.get("degrade_category")
        }
    )
    if divergences:
        first = divergences[0]
        raise DiagnosticError(
            "E_INCONSISTENT",
            "result_validation",
            "cross-precision or block/path validation failed",
            degrade_category="reject_result",
            details={"checks": checks, "degradation_categories": degradation_categories},
            first_divergence=first,
        )
    return {
        "status": "passed",
        "tolerance": tolerance,
        "checks": checks,
        "degradation_categories": degradation_categories,
        "first_divergence": None,
    }

