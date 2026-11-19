"""SciPy-backed descriptive statistics for error distributions."""
from __future__ import annotations

import numpy as np
from scipy import stats


def distribution_shape(errors: np.ndarray) -> dict[str, float]:
    """Return skew/kurtosis of an error distribution via SciPy."""
    arr = np.asarray(errors, dtype=np.float64)
    if arr.size < 4 or np.allclose(arr, arr[0]):
        return {"skew": 0.0, "excess_kurtosis": 0.0, "std": float(np.std(arr))}
    return {
        "skew": float(stats.skew(arr, bias=False)),
        "excess_kurtosis": float(stats.kurtosis(arr, bias=False)),
        "std": float(np.std(arr, ddof=1)),
    }


def confidence_interval_mean(errors: np.ndarray, confidence: float = 0.95) -> dict[str, float]:
    """Two-sided t-distribution confidence interval for the mean error."""
    arr = np.asarray(errors, dtype=np.float64)
    n = arr.size
    if n < 2 or np.allclose(arr, arr[0]):
        center = float(np.mean(arr)) if n else 0.0
        return {"confidence": confidence, "lower": center, "upper": center,
                "mean": center}
    mean = float(np.mean(arr))
    sem = float(stats.sem(arr))
    half_width = float(stats.t.ppf((1 + confidence) / 2.0, n - 1) * sem)
    return {"confidence": confidence, "lower": mean - half_width,
            "upper": mean + half_width, "mean": mean}
