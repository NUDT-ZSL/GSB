from __future__ import annotations

from typing import Any

import numpy as np

from .errors import DomainError, ErrorCode
from .models import Job, ToleranceConfig
from .quant_conv import reference_tensors

STAGE_ORDER = (
    "quantized_input",
    "fixed_mac",
    "accumulator_reduction",
    "dequantized_output",
)
CONTRIBUTING_STAGES = STAGE_ORDER[:3]
EPSILON = 1e-12


def _quantiles(values: np.ndarray) -> dict[str, float]:
    if values.size == 0:
        return {"p50": 0.0, "p90": 0.0, "p99": 0.0}
    p50, p90, p99 = np.quantile(values, [0.5, 0.9, 0.99])
    return {"p50": float(p50), "p90": float(p90), "p99": float(p99)}


def _top_locations(
    abs_error: np.ndarray,
    reference: np.ndarray,
    actual: np.ndarray,
    limit: int = 5,
) -> list[dict[str, Any]]:
    flat = abs_error.reshape(-1)
    count = min(limit, flat.size)
    if count == 0:
        return []
    indices = np.argpartition(flat, -count)[-count:]
    indices = indices[np.lexsort((indices, -flat[indices]))]
    locations: list[dict[str, Any]] = []
    ref_flat = reference.reshape(-1)
    actual_flat = actual.reshape(-1)
    for flat_index in indices.tolist():
        coordinate = [int(item) for item in np.unravel_index(flat_index, abs_error.shape)]
        ref_value = float(ref_flat[flat_index])
        actual_value = float(actual_flat[flat_index])
        locations.append(
            {
                "index": coordinate,
                "reference": ref_value,
                "actual": actual_value,
                "abs_diff": float(abs(actual_value - ref_value)),
                "signed_diff": float(actual_value - ref_value),
            }
        )
    return locations


def _stage_metrics(
    name: str,
    reference: np.ndarray,
    actual: np.ndarray,
    previous_max_abs: float | None,
    quantization_max_abs: float | None,
) -> tuple[dict[str, Any], float]:
    if reference.shape != actual.shape:
        raise DomainError(
            ErrorCode.E_INCONSISTENT,
            f"diagnostic tensor shape mismatch at stage {name}",
            stage=name,
            detail={"reference": list(reference.shape), "actual": list(actual.shape)},
        )
    reference64 = reference.astype(np.float64)
    actual64 = actual.astype(np.float64)
    signed = actual64 - reference64
    abs_error = np.abs(signed)
    relative = abs_error / (np.abs(reference64) + EPSILON)
    max_abs = float(np.max(abs_error)) if abs_error.size else 0.0

    if previous_max_abs is None:
        amplification_previous: float | None = 1.0
    elif previous_max_abs == 0.0:
        amplification_previous = 0.0 if max_abs == 0.0 else None
    else:
        amplification_previous = max_abs / previous_max_abs

    if quantization_max_abs is None:
        amplification_quantization: float | None = 1.0
    elif quantization_max_abs == 0.0:
        amplification_quantization = 0.0 if max_abs == 0.0 else None
    else:
        amplification_quantization = max_abs / quantization_max_abs

    metrics = {
        "stage": name,
        "element_count": int(abs_error.size),
        "max_abs_error": max_abs,
        "mean_abs_error": float(np.mean(abs_error)) if abs_error.size else 0.0,
        "max_relative_error": float(np.max(relative)) if relative.size else 0.0,
        "mean_relative_error": float(np.mean(relative)) if relative.size else 0.0,
        "abs_error_quantiles": _quantiles(abs_error),
        "relative_error_quantiles": _quantiles(relative),
        "amplification_vs_previous": amplification_previous,
        "amplification_vs_quantization": amplification_quantization,
        "top_abs_errors": _top_locations(abs_error, reference64, actual64),
    }
    return metrics, max_abs


def diagnose_job(
    job: Job,
    actual_output: np.ndarray,
    tolerance: ToleranceConfig,
) -> dict[str, Any]:
    tensors = reference_tensors(job)
    expected_shape = job.output_shape
    if tuple(actual_output.shape) != expected_shape:
        raise DomainError(
            ErrorCode.E_INCONSISTENT,
            "actual output shape does not match job output shape",
            job_id=job.job_id,
            stage="diagnostics",
            detail={
                "expected": list(expected_shape),
                "actual": list(actual_output.shape),
            },
        )
    arrays = {
        "quantized_input": (
            tensors["reference_dequant"],
            tensors["reference_float"],
        ),
        "fixed_mac": (tensors["reference_dequant"], tensors["direct_scaled"]),
        "accumulator_reduction": (
            tensors["direct_scaled"],
            actual_output.astype(np.float64),
        ),
        "dequantized_output": (
            tensors["reference_dequant"],
            actual_output.astype(np.float64),
        ),
    }
    stages: list[dict[str, Any]] = []
    previous_max: float | None = None
    quantization_max: float | None = None
    for name in STAGE_ORDER:
        reference, actual = arrays[name]
        metrics, max_abs = _stage_metrics(
            name, reference, actual, previous_max, quantization_max
        )
        if name == "quantized_input":
            quantization_max = max_abs
        stages.append(metrics)
        previous_max = max_abs

    by_stage = {stage["stage"]: stage for stage in stages}
    dominant = max(
        CONTRIBUTING_STAGES,
        key=lambda name: (
            by_stage[name]["max_abs_error"],
            -STAGE_ORDER.index(name),
        ),
    )
    dominant_stage = by_stage[dominant]
    reference = tensors["reference_dequant"].astype(np.float64)
    actual64 = actual_output.astype(np.float64)
    diff = np.abs(actual64 - reference)
    allowed = tolerance.atol + tolerance.rtol * np.abs(reference)
    violating_mask = diff > allowed
    violating_indices = np.flatnonzero(violating_mask.reshape(-1))
    violations: list[dict[str, Any]] = []
    for flat_index in violating_indices[: tolerance.max_violations].tolist():
        coordinate = [int(item) for item in np.unravel_index(flat_index, expected_shape)]
        reference_value = float(reference.reshape(-1)[flat_index])
        actual_value = float(actual64.reshape(-1)[flat_index])
        diff_value = float(abs(actual_value - reference_value))
        violations.append(
            {
                "job_id": job.job_id,
                "index": coordinate,
                "reference": reference_value,
                "actual": actual_value,
                "abs_diff": diff_value,
                "relative_diff": diff_value / (abs(reference_value) + EPSILON),
                "allowed": float(allowed.reshape(-1)[flat_index]),
                "stage": "dequantized_output",
            }
        )
    input_q = tensors["input_q"]
    weight_q = tensors["weight_q"]
    return {
        "job_id": job.job_id,
        "quant_tier": job.quant_tier,
        "quant_scale": job.quant_scale,
        "zero_point": job.zero_point,
        "stages": stages,
        "dominant_stage": dominant,
        "dominant_reason": (
            f"{dominant} has the largest stage-local max absolute error "
            f"({dominant_stage['max_abs_error']}); inspect its top_abs_errors"
        ),
        "dominant_trace": dominant_stage["top_abs_errors"],
        "quantized_input_range": [int(input_q.min()), int(input_q.max())],
        "quantized_weight_range": [int(weight_q.min()), int(weight_q.max())],
        "output_reference": "float64 convolution over dequantized int8 inputs and weights",
        "tolerance": tolerance.to_dict(),
        "tolerance_ok": len(violating_indices) == 0,
        "violation_count": int(len(violating_indices)),
        "violations_truncated": bool(len(violating_indices) > tolerance.max_violations),
        "violations": violations,
        "reference_checksum": _array_checksum(tensors["reference_dequant"]),
    }


def _array_checksum(values: np.ndarray) -> str:
    import hashlib

    return hashlib.sha256(
        np.ascontiguousarray(values.astype("<f8")).tobytes()
    ).hexdigest()


def build_error_spectrum(diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [item["job_id"] for item in diagnostics if not item["tolerance_ok"]]
    return {
        "jobs": diagnostics,
        "tolerance_failed_job_ids": failed,
        "all_tolerance_ok": not failed,
        "dominant_stage_by_job": {
            item["job_id"]: item["dominant_stage"] for item in diagnostics
        },
    }

