"""Element-wise adjudication between chunked and whole-segment results."""

import numpy as np

from .errors import ErrorCode, OpChainError

TOLERANCES = {
    "float64": {"atol": 1e-12, "rtol": 1e-12},
    "float32": {"atol": 1e-6, "rtol": 1e-5},
}


def tolerance_profile(precision, eps):
    profile = TOLERANCES.get(precision, TOLERANCES["float64"])
    return {
        "precision": precision,
        "atol": float(profile["atol"]),
        "rtol": float(profile["rtol"]),
        "relative_denominator": "max(abs(reference), eps)",
        "eps": float(eps),
    }


def compare_arrays(chunked, reference, precision, eps, limit=10):
    """Compare in float64; return deviation stats and out-of-tolerance positions."""
    actual = np.ascontiguousarray(chunked, dtype=np.float64)
    expected = np.ascontiguousarray(reference, dtype=np.float64)
    if actual.shape != expected.shape:
        raise OpChainError(
            ErrorCode.INCONSISTENT,
            "shape mismatch between chunked and reference outputs",
            {"chunked_shape": list(actual.shape), "reference_shape": list(expected.shape)},
        )

    absolute = np.abs(actual - expected)
    denominator = np.maximum(np.abs(expected), eps)
    relative = absolute / denominator
    max_abs_index = int(np.argmax(absolute))
    max_rel_index = int(np.argmax(relative))

    profile = tolerance_profile(precision, eps)
    threshold = profile["atol"] + profile["rtol"] * np.abs(expected)
    violation_mask = absolute > threshold
    violation_indices = np.nonzero(violation_mask.reshape(-1))[0]

    violations = []
    for flat_index in violation_indices[:limit]:
        index = int(flat_index)
        multi_index = np.unravel_index(index, expected.shape)
        violations.append(
            {
                "flat_index": index,
                "multi_index": list(int(axis) for axis in multi_index),
                "chunked": float(actual.reshape(-1)[index]),
                "reference": float(expected.reshape(-1)[index]),
                "abs_diff": float(absolute.reshape(-1)[index]),
                "rel_diff": float(relative.reshape(-1)[index]),
            }
        )

    return {
        "tolerance": profile,
        "element_count": int(expected.size),
        "max_abs_diff": float(absolute.reshape(-1)[max_abs_index]),
        "max_abs_flat_index": max_abs_index,
        "max_rel_diff": float(relative.reshape(-1)[max_rel_index]),
        "max_rel_flat_index": max_rel_index,
        "violation_count": int(violation_indices.size),
        "violations": violations,
    }


def adjudicate(chunked_arrays, reference_stages, final_chunked, final_reference,
               precision, eps):
    """Adjudicate every stage plus the final output."""
    stage_reports = []
    for chunked_array, reference_record in zip(chunked_arrays, reference_stages):
        report = compare_arrays(
            chunked_array,
            reference_record["array"],
            precision,
            eps,
        )
        report["stage"] = reference_record["stage"]
        report["op"] = reference_record["op"]
        stage_reports.append(report)

    final_report = compare_arrays(
        final_chunked, final_reference, precision, eps
    )
    final_report["stage"] = "final"
    all_reports = stage_reports + [final_report]
    total_violations = sum(report["violation_count"] for report in all_reports)
    if total_violations:
        first = next(report for report in all_reports if report["violation_count"])
        raise OpChainError(
            ErrorCode.TOLERANCE_EXCEEDED,
            "chunked output deviates from the whole-segment reference beyond tolerance",
            {
                "precision": precision,
                "tolerance": tolerance_profile(precision, eps),
                "first_violation_stage": first["stage"],
                "first_violation": first["violations"][0],
                "final_report": final_report,
            },
        )
    return {"stage_reports": stage_reports, "final_report": final_report}
