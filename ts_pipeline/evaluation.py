"""Stable threshold decisions and independently recomputable metrics."""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

from .utils import guard_subnormal, ordered_sum


def threshold_decision(
    error: float,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    evaluation = config["evaluation"]
    guard = bool(config["training"]["subnormal_guard"])
    threshold = float(evaluation["threshold"])
    tolerance = float(evaluation["tolerance"])
    strategy = str(evaluation["threshold_strategy"])
    value = abs(guard_subnormal(float(error), guard))
    distance = value - threshold
    protected = tolerance > 0.0 and abs(distance) <= tolerance
    if not math.isfinite(value):
        return {
            "strategy": strategy,
            "threshold": threshold,
            "tolerance": tolerance,
            "evaluated_error": value,
            "passed": False,
            "protected": protected,
            "skipped": True,
            "skip_reason": "non_finite_error",
            "distance_to_threshold": distance,
        }
    if strategy == "strict":
        passed = value < threshold
        rule = "abs_error < threshold"
    elif strategy == "tolerance_band":
        midpoint = threshold + tolerance / 2.0
        passed = value <= midpoint
        rule = "abs_error <= threshold + tolerance/2"
    else:
        raise ValueError(f"unsupported threshold strategy: {strategy}")
    return {
        "strategy": strategy,
        "threshold": threshold,
        "tolerance": tolerance,
        "evaluated_error": value,
        "rule": rule,
        "passed": bool(passed),
        "protected": bool(protected),
        "skipped": False,
        "skip_reason": None,
        "distance_to_threshold": float(distance),
    }


def evaluate_metrics(
    steps: Iterable[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Aggregate metrics only from per-window/per-step detail rows."""

    materialized = list(steps)
    metric_epsilon = float(config["evaluation"]["metric_epsilon"])
    compensated = bool(config["training"]["compensated_sum"])
    guard = bool(config["training"]["subnormal_guard"])
    absolute_errors: list[float] = []
    squared_errors: list[float] = []
    ape_terms: list[float] = []
    smape_terms: list[float] = []
    pass_values: list[float] = []
    coverage_values: list[float] = []
    for step in materialized:
        error = guard_subnormal(float(step["error"]), guard)
        true_value = float(step["true_value"])
        prediction = float(step["prediction"])
        absolute = abs(error)
        absolute_errors.append(absolute)
        squared_errors.append(error * error)
        ape_terms.append(
            absolute / max(abs(true_value), metric_epsilon)
        )
        smape_terms.append(
            2.0
            * absolute
            / max(abs(true_value) + abs(prediction), metric_epsilon)
        )
        pass_values.append(1.0 if step["decision"]["passed"] else 0.0)
        lower = float(step["interval_lower"])
        upper = float(step["interval_upper"])
        coverage_values.append(
            1.0 if lower <= true_value <= upper else 0.0
        )

    count = len(materialized)
    if count == 0:
        return {
            "step_count": 0,
            "mae": 0.0,
            "rmse": 0.0,
            "mape_percent": 0.0,
            "smape_percent": 0.0,
            "max_abs_error": 0.0,
            "threshold_pass_rate": 0.0,
            "interval_coverage_rate": 0.0,
        }
    mae = ordered_sum(absolute_errors, compensated) / count
    mse = ordered_sum(squared_errors, compensated) / count
    mape = 100.0 * ordered_sum(ape_terms, compensated) / count
    smape = 100.0 * ordered_sum(smape_terms, compensated) / count
    requested = set(config["evaluation"]["metrics"])
    metrics: dict[str, Any] = {
        "step_count": count,
        "mae": float(mae),
        "rmse": float(math.sqrt(max(0.0, mse))),
        "mape_percent": float(mape),
        "smape_percent": float(smape),
        "max_abs_error": float(max(absolute_errors, default=0.0)),
        "threshold_pass_rate": float(
            ordered_sum(pass_values, compensated) / count
        ),
        "interval_coverage_rate": float(
            ordered_sum(coverage_values, compensated) / count
        ),
    }
    metrics["requested"] = sorted(requested)
    metrics["recompute_formula"] = {
        "aggregation": "explicit order of prediction detail rows",
        "mape_percent": "100 * mean(abs(error) / max(abs(true), epsilon))",
        "smape_percent": "100 * mean(2*abs(error)/max(abs(true)+abs(pred), epsilon))",
    }
    metrics["digest_relevant"] = {
        "mae": metrics["mae"],
        "rmse": metrics["rmse"],
        "mape_percent": metrics["mape_percent"],
        "smape_percent": metrics["smape_percent"],
        "max_abs_error": metrics["max_abs_error"],
        "threshold_pass_rate": metrics["threshold_pass_rate"],
    }
    return metrics
