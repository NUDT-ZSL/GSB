from __future__ import annotations

from typing import Any

import numpy as np

from .errors import OpVerifyError


DEFAULT_TOLERANCE = {"rtol": 1e-9, "atol": 1e-10}


def compare_outputs(
    expected: np.ndarray,
    actual: np.ndarray,
    *,
    rtol: float | None = None,
    atol: float | None = None,
) -> dict[str, Any]:
    ref = np.asarray(expected, dtype=np.float64)
    got = np.asarray(actual, dtype=np.float64)
    tolerance = DEFAULT_TOLERANCE
    rtol_value = tolerance["rtol"] if rtol is None else rtol
    atol_value = tolerance["atol"] if atol is None else atol

    if ref.shape != got.shape:
        error = OpVerifyError(
            "E_TOLERANCE_EXCEEDED",
            "chunk output shape does not match reference",
            field="$.output_shape",
            details={
                "expected_shape": list(ref.shape),
                "actual_shape": list(got.shape),
                "first_mismatch": {"path": "$", "reason": "shape"},
            },
        )
        raise error

    difference = np.abs(ref - got)
    threshold = atol_value + rtol_value * np.abs(ref)
    mismatch = difference > threshold
    mismatch |= ~np.isfinite(got)
    mismatch |= ~np.isfinite(ref)

    max_abs = float(np.max(difference)) if difference.size else 0.0
    denom = np.maximum(np.abs(ref), atol_value)
    max_rel = float(np.max(difference / denom)) if difference.size else 0.0

    if np.any(mismatch):
        flat_index = int(np.flatnonzero(mismatch.ravel())[0])
        position = np.unravel_index(flat_index, ref.shape)
        first_position = [int(item) for item in position]
        details = {
            "first_mismatch": {
                "position": first_position,
                "flat_index": flat_index,
                "expected": float(ref[position]),
                "actual": float(got[position]),
                "absolute_error": float(difference[position]),
                "threshold": float(threshold[position]),
            },
            "mismatch_count": int(np.count_nonzero(mismatch)),
            "max_absolute_error": max_abs,
            "max_relative_error": max_rel,
            "rtol": rtol_value,
            "atol": atol_value,
        }
        raise OpVerifyError(
            "E_TOLERANCE_EXCEEDED",
            "chunk output differs from float64 reference beyond tolerance",
            field="$.output",
            details=details,
        )

    return {
        "ok": True,
        "mismatch_count": 0,
        "max_absolute_error": max_abs,
        "max_relative_error": max_rel,
        "rtol": rtol_value,
        "atol": atol_value,
    }


def declared_tolerance(task: dict[str, Any]) -> dict[str, float]:
    custom = task.get("tolerance")
    if isinstance(custom, dict):
        return {
            "rtol": float(custom.get("rtol", DEFAULT_TOLERANCE["rtol"])),
            "atol": float(custom.get("atol", DEFAULT_TOLERANCE["atol"])),
        }
    return dict(DEFAULT_TOLERANCE)
