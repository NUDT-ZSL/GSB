"""Element-wise comparison against a one-shot float64 reference."""

from __future__ import annotations

from typing import Any

import numpy as np

from .models import ToleranceConfig
from .util import sha256_json


def compare_arrays(
    candidate: np.ndarray,
    reference: np.ndarray,
    tolerance: ToleranceConfig,
) -> dict[str, Any]:
    candidate64 = np.asarray(candidate, dtype=np.float64)
    reference64 = np.asarray(reference, dtype=np.float64)
    if candidate64.shape != reference64.shape:
        raise ValueError("candidate and reference shapes differ")

    absolute = np.abs(candidate64 - reference64)
    relative = absolute / np.maximum(
        np.abs(reference64), tolerance.relative_floor
    )
    allowed = tolerance.abs_tol + tolerance.rel_tol * np.abs(reference64)
    violation_mask = ~(absolute <= allowed)

    flat_absolute = absolute.reshape(-1)
    flat_relative = relative.reshape(-1)
    flat_candidate = candidate64.reshape(-1)
    flat_reference = reference64.reshape(-1)
    flat_violation = violation_mask.reshape(-1)

    max_absolute_index = int(np.argmax(flat_absolute))
    max_relative_index = int(np.argmax(flat_relative))
    positions: list[dict[str, Any]] = []
    for flat_index in np.flatnonzero(flat_violation):
        index = int(flat_index)
        positions.append(
            {
                "flat_index": index,
                "multi_index": [int(item) for item in np.unravel_index(index, reference64.shape)],
                "candidate_value": float(flat_candidate[index]),
                "reference_value": float(flat_reference[index]),
                "absolute_deviation": float(flat_absolute[index]),
                "relative_deviation": float(flat_relative[index]),
            }
        )

    report: dict[str, Any] = {
        "abs_tolerance": tolerance.abs_tol,
        "rel_tolerance": tolerance.rel_tol,
        "relative_floor": tolerance.relative_floor,
        "compared_elements": int(reference64.size),
        "max_absolute_deviation": float(flat_absolute[max_absolute_index]),
        "max_absolute_flat_index": max_absolute_index,
        "max_relative_deviation": float(flat_relative[max_relative_index]),
        "max_relative_flat_index": max_relative_index,
        "violation_count": int(np.count_nonzero(flat_violation)),
        "violations": positions,
        "within_tolerance": bool(not np.any(flat_violation)),
    }
    report["spectrum_sha256"] = sha256_json(report)
    return report
