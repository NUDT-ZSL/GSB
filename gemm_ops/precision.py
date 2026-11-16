"""Numerical error summaries and accumulation-order comparisons."""

from __future__ import annotations

from typing import Any

import numpy as np


def _distribution(values: np.ndarray) -> dict[str, float | int]:
    if values.size == 0:
        return {
            "count": 0,
            "mean": 0.0,
            "p50": 0.0,
            "p90": 0.0,
            "p99": 0.0,
            "max": 0.0,
        }
    return {
        "count": int(values.size),
        "mean": float(np.mean(values)),
        "p50": float(np.quantile(values, 0.50)),
        "p90": float(np.quantile(values, 0.90)),
        "p99": float(np.quantile(values, 0.99)),
        "max": float(np.max(values)),
    }


def error_summary(
    actual: np.ndarray,
    reference: np.ndarray,
    *,
    relative_epsilon: float,
) -> dict[str, Any]:
    actual64 = np.asarray(actual, dtype=np.float64)
    reference64 = np.asarray(reference, dtype=np.float64)
    if actual64.shape != reference64.shape:
        raise ValueError("actual and reference shapes differ")
    absolute = np.abs(actual64 - reference64)
    denominator = np.maximum(np.abs(reference64), relative_epsilon)
    relative = absolute / denominator
    return {
        "definition": {
            "absolute_error": "abs(float32_path - float64_reference)",
            "relative_error": "abs(float32_path - float64_reference) / max(abs(float64_reference), relative_epsilon)",
            "relative_epsilon": relative_epsilon,
            "quantile_method": "linear",
        },
        "max_absolute_error": float(np.max(absolute)) if absolute.size else 0.0,
        "max_relative_error": float(np.max(relative)) if relative.size else 0.0,
        "mean_absolute_error": float(np.mean(absolute)) if absolute.size else 0.0,
        "rms_absolute_error": float(np.sqrt(np.mean(absolute**2))) if absolute.size else 0.0,
        "absolute_error_distribution": _distribution(absolute.reshape(-1)),
        "relative_error_distribution": _distribution(relative.reshape(-1)),
        "nonfinite_output_count": int(np.count_nonzero(~np.isfinite(actual64))),
    }


def max_abs_difference(left: np.ndarray, right: np.ndarray) -> float:
    left64 = np.asarray(left, dtype=np.float64)
    right64 = np.asarray(right, dtype=np.float64)
    if left64.shape != right64.shape:
        return float("inf")
    if left64.size == 0:
        return 0.0
    return float(np.max(np.abs(left64 - right64)))


def arrays_equal(left: np.ndarray, right: np.ndarray) -> bool:
    left_arr = np.asarray(left)
    right_arr = np.asarray(right)
    return left_arr.shape == right_arr.shape and bool(np.array_equal(left_arr, right_arr))


def pair_key(loop_a: str, loop_b: str) -> str:
    return "|".join(sorted((loop_a, loop_b)))
