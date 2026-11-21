"""Element-wise reference comparison and tolerance accounting."""

from __future__ import annotations

from typing import Any

import numpy as np

from .schema import NormalizedJob, Tolerance


def effective_tolerance(
    tolerance: Tolerance, *, dtype: str
) -> tuple[Tolerance, dict[str, Any]]:
    """Apply a documented precision floor for the float32 execution path."""

    if dtype != "float32":
        return tolerance, {
            "dtype_floor_applied": False,
            "atol": tolerance.atol,
            "rtol": tolerance.rtol,
        }
    effective = Tolerance(
        atol=max(tolerance.atol, 1e-5),
        rtol=max(tolerance.rtol, 1e-4),
    )
    return effective, {
        "dtype_floor_applied": True,
        "atol": effective.atol,
        "rtol": effective.rtol,
        "float32_atol_floor": 1e-5,
        "float32_rtol_floor": 1e-4,
    }


def _location(index: tuple[int, ...], job: NormalizedJob) -> dict[str, Any]:
    axes = (
        ("n", "o", "h", "w")
        if job.family == "conv"
        else ("n", "h", "w", "c")
    )
    return {axis: int(value) for axis, value in zip(axes, index)}


def compare_outputs(
    job: NormalizedJob,
    actual: np.ndarray,
    reference: np.ndarray,
    tolerance: Tolerance,
    *,
    max_records: int = 128,
) -> dict[str, Any]:
    actual64 = np.asarray(actual, dtype=np.float64)
    reference64 = np.asarray(reference, dtype=np.float64)
    if actual64.shape != reference64.shape:
        raise ValueError("actual and reference shapes differ")

    difference = actual64 - reference64
    absolute = np.abs(difference)
    denominator = np.maximum(np.abs(reference64), 1e-12)
    relative = absolute / denominator
    threshold = tolerance.atol + tolerance.rtol * np.abs(reference64)
    violation_mask = absolute > threshold
    violation_count = int(np.count_nonzero(violation_mask))

    max_abs_flat = int(np.argmax(absolute)) if absolute.size else 0
    max_abs_index = np.unravel_index(max_abs_flat, absolute.shape)
    max_rel_flat = int(np.argmax(relative)) if relative.size else 0
    max_rel_index = np.unravel_index(max_rel_flat, relative.shape)

    violation_records: list[dict[str, Any]] = []
    for flat_index in np.flatnonzero(violation_mask.ravel())[:max_records]:
        index = np.unravel_index(int(flat_index), absolute.shape)
        violation_records.append(
            {
                "location": _location(tuple(int(item) for item in index), job),
                "flat_index": int(flat_index),
                "actual": float(actual64[index]),
                "reference": float(reference64[index]),
                "abs_error": float(absolute[index]),
                "rel_error": float(relative[index]),
                "threshold": float(threshold[index]),
            }
        )

    return {
        "tolerance": tolerance.as_dict(),
        "relative_denominator_floor": 1e-12,
        "max_abs_error": float(absolute[max_abs_index]) if absolute.size else 0.0,
        "max_abs_location": _location(
            tuple(int(item) for item in max_abs_index), job
        )
        if absolute.size
        else None,
        "max_rel_error": float(relative[max_rel_index]) if relative.size else 0.0,
        "max_rel_location": _location(
            tuple(int(item) for item in max_rel_index), job
        )
        if relative.size
        else None,
        "mean_abs_error": float(np.mean(absolute)) if absolute.size else 0.0,
        "sum_abs_error": float(np.sum(absolute)) if absolute.size else 0.0,
        "violation_count": violation_count,
        "within_tolerance": violation_count == 0,
        "violations": violation_records,
        "violations_truncated": violation_count > len(violation_records),
        "abs_error_values": absolute.ravel().tolist(),
        "abs_error_shape": list(absolute.shape),
    }
