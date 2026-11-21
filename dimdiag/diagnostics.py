"""Stage-wise error spectrum and numerically stable threshold decisions."""

from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np

from .core import ExecutionResult
from .errors import E_TOLERANCE_EXCEEDED, BusinessError
from .precision import PrecisionOps


SOURCE_BY_STAGE = {
    "standardization": "standardization",
    "model_construction": "projection_matrix_construction",
    "coordinates": "coordinate_computation",
    "two_stage_intermediate": "two_stage_combination",
}


def _flatten_with_offsets(arrays: list[np.ndarray]) -> tuple[np.ndarray, list[int]]:
    offsets: list[int] = []
    flattened: list[np.ndarray] = []
    cursor = 0
    for array in arrays:
        offsets.append(cursor)
        flat = np.asarray(array).reshape(-1)
        flattened.append(flat)
        cursor += int(flat.size)
    if flattened:
        return np.concatenate(flattened), offsets
    return np.asarray([], dtype=np.float64), offsets


def _array_element(index: int, shape: tuple[int, ...]) -> dict[str, Any]:
    if len(shape) == 1:
        return {"flat_index": index, "row": index, "column": None}
    if len(shape) == 2:
        row, column = np.unravel_index(index, shape)
        return {"flat_index": index, "row": int(row), "column": int(column)}
    return {"flat_index": index, "row": None, "column": None}


def _summary(values: np.ndarray) -> dict[str, float | None]:
    if values.size == 0:
        return {
            "min": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "p95": 0.0,
            "rms": 0.0,
        }
    return {
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "mean": float(np.mean(values)),
        "p95": float(np.percentile(values, 95)),
        "rms": float(np.sqrt(np.mean(np.square(values)))),
    }


def compare_stage(
    *,
    stage: str,
    observed_arrays: list[np.ndarray],
    reference_arrays: list[np.ndarray],
    abs_limit: float,
    rel_limit: float,
    subnormal_floor: float,
    top_count: int,
    observed_shapes: list[tuple[int, ...]] | None = None,
) -> dict[str, Any]:
    observed, _ = _flatten_with_offsets(observed_arrays)
    reference, _ = _flatten_with_offsets(reference_arrays)
    if observed.shape != reference.shape:
        raise ValueError(
            f"stage {stage} shape mismatch: {observed.shape} vs {reference.shape}"
        )
    observed = observed.astype(np.float64)
    reference = reference.astype(np.float64)
    absolute = np.abs(observed - reference)
    reference_rms = (
        float(np.sqrt(np.mean(np.square(reference)))) if reference.size else 0.0
    )
    denominator = np.maximum(
        np.maximum(np.abs(reference), reference_rms),
        float(subnormal_floor),
    )
    relative = absolute / denominator
    abs_summary = _summary(absolute)
    rel_summary = _summary(relative)

    shape = (
        observed_shapes[0]
        if observed_shapes and len(observed_shapes) == 1
        else (int(observed.size),)
    )
    order = np.argsort(-absolute, kind="stable")
    top_elements: list[dict[str, Any]] = []
    for flat_index in order[:top_count]:
        entry = _array_element(int(flat_index), shape)
        entry.update(
            {
                "reference": float(reference[flat_index]),
                "observed": float(observed[flat_index]),
                "abs_error": float(absolute[flat_index]),
                "relative_error": float(relative[flat_index]),
            }
        )
        top_elements.append(entry)

    abs_bad = absolute > float(abs_limit)
    rel_bad = relative > float(rel_limit)
    bad = abs_bad | rel_bad
    violations: list[dict[str, Any]] = []
    for flat_index in np.flatnonzero(bad)[:top_count]:
        entry = _array_element(int(flat_index), shape)
        entry.update(
            {
                "stage": stage,
                "reference": float(reference[flat_index]),
                "observed": float(observed[flat_index]),
                "abs_error": float(absolute[flat_index]),
                "relative_error": float(relative[flat_index]),
                "abs_limit": float(abs_limit),
                "rel_limit": float(rel_limit),
                "exceeded": (
                    "absolute"
                    if bool(abs_bad[flat_index]) and not bool(rel_bad[flat_index])
                    else "relative"
                    if bool(rel_bad[flat_index]) and not bool(abs_bad[flat_index])
                    else "absolute_and_relative"
                ),
            }
        )
        violations.append(entry)

    return {
        "stage": stage,
        "source": SOURCE_BY_STAGE[stage],
        "element_count": int(absolute.size),
        "tolerance": {
            "max_abs": float(abs_limit),
            "max_rel": float(rel_limit),
        },
        "absolute_error": abs_summary,
        "relative_error": rel_summary,
        "error_interval": {
            "absolute_min": abs_summary["min"],
            "absolute_max": abs_summary["max"],
            "relative_min": rel_summary["min"],
            "relative_max": rel_summary["max"],
        },
        "relative_denominator": {
            "formula": "max(abs(reference), reference_stage_rms, subnormal_floor)",
            "reference_stage_rms": reference_rms,
        },
        "error_amplification_ratio": None,
        "top_elements": top_elements,
        "violations": violations,
        "passed": not violations,
    }


def _model_arrays(result: ExecutionResult, *, as_float: bool) -> list[np.ndarray]:
    ops = PrecisionOps(result.precision)
    arrays: list[np.ndarray] = []
    if result.pca_components is not None:
        arrays.append(
            ops.to_float(result.pca_components) if as_float else result.pca_components
        )
    for stage in result.projection_stages:
        if stage["stage"] == "random_projection":
            arrays.append(ops.to_float(stage["matrix"]) if as_float else stage["matrix"])
    return arrays


def compute_error_spectrum(
    observed: ExecutionResult,
    reference: ExecutionResult,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    tolerance = config["error_tolerance"]
    evaluation = config["evaluation"]
    top_count = int(config["diagnostics"]["top_error_elements"])
    floor = float(evaluation["subnormal_floor"])
    stages: list[dict[str, Any]] = []
    observed_ops = PrecisionOps(observed.precision)
    reference_ops = PrecisionOps(reference.precision)

    standardization_observed = [
        observed_ops.to_float(observed.numeric_mean),
        observed_ops.to_float(observed.numeric_scale),
        observed_ops.to_float(observed.numeric_standardized),
    ]
    standardization_reference = [
        reference_ops.to_float(reference.numeric_mean),
        reference_ops.to_float(reference.numeric_scale),
        reference_ops.to_float(reference.numeric_standardized),
    ]
    stages.append(
        compare_stage(
            stage="standardization",
            observed_arrays=standardization_observed,
            reference_arrays=standardization_reference,
            abs_limit=float(tolerance["standardization_max_abs"]),
            rel_limit=float(tolerance["standardization_max_rel"]),
            subnormal_floor=floor,
            top_count=top_count,
        )
    )

    stages.append(
        compare_stage(
            stage="model_construction",
            observed_arrays=_model_arrays(observed, as_float=True),
            reference_arrays=_model_arrays(reference, as_float=True),
            abs_limit=float(tolerance["model_construction_max_abs"]),
            rel_limit=float(tolerance["model_construction_max_rel"]),
            subnormal_floor=floor,
            top_count=top_count,
        )
    )

    if (
        observed.effective_method == "pca_random_projection"
        and observed.intermediate_coordinates is not None
        and reference.intermediate_coordinates is not None
    ):
        stages.append(
            compare_stage(
                stage="two_stage_intermediate",
                observed_arrays=[
                    observed_ops.to_float(observed.intermediate_coordinates)
                ],
                reference_arrays=[
                    reference_ops.to_float(reference.intermediate_coordinates)
                ],
                abs_limit=float(tolerance["two_stage_max_abs"]),
                rel_limit=float(tolerance["two_stage_max_rel"]),
                subnormal_floor=floor,
                top_count=top_count,
                observed_shapes=[observed.intermediate_coordinates.shape],
            )
        )

    stages.append(
        compare_stage(
            stage="coordinates",
            observed_arrays=[observed_ops.to_float(observed.coordinates)],
            reference_arrays=[reference_ops.to_float(reference.coordinates)],
            abs_limit=float(tolerance["coordinates_max_abs"]),
            rel_limit=float(tolerance["coordinates_max_rel"]),
            subnormal_floor=floor,
            top_count=top_count,
            observed_shapes=[observed.coordinates.shape],
        )
    )

    previous_rms: float | None = None
    for stage in stages:
        current_rms = float(stage["absolute_error"]["rms"])
        if previous_rms is None:
            ratio = None
            state = "baseline"
        elif previous_rms <= floor and current_rms <= floor:
            ratio = 1.0
            state = "finite"
        elif previous_rms <= floor:
            ratio = None
            state = "unbounded"
        else:
            ratio = current_rms / previous_rms
            state = "finite"
        stage["error_amplification_ratio"] = ratio
        stage["error_amplification_state"] = state
        previous_rms = current_rms

    dominant_stage = "none"
    dominant_source = "none"
    maximum_rms = 0.0
    for stage in stages:
        rms = float(stage["absolute_error"]["rms"])
        if rms > maximum_rms:
            maximum_rms = rms
            dominant_stage = str(stage["stage"])
            dominant_source = str(stage["source"])

    violations = [
        violation
        for stage in stages
        for violation in stage["violations"]
    ]
    return {
        "reference_precision": "float64",
        "observed_precision": observed.precision.to_dict(),
        "subnormal_floor": floor,
        "stages": stages,
        "dominant_stage": dominant_stage,
        "dominant_source": dominant_source,
        "dominant_abs_rms": maximum_rms,
        "tolerance_passed": not violations,
        "violations": violations,
        "notes": (
            "Absolute and relative errors use float64 execution on the same "
            "input, effective method, and resource decision as reference."
        ),
    }


def enforce_error_tolerance(spectrum: Mapping[str, Any]) -> None:
    violations = list(spectrum.get("violations", []))
    if not violations:
        return
    raise BusinessError(
        E_TOLERANCE_EXCEEDED,
        "stage deviation exceeds the configured error tolerance",
        {
            "violation_count": len(violations),
            "violations": violations[:16],
            "dominant_stage": spectrum.get("dominant_stage"),
            "dominant_source": spectrum.get("dominant_source"),
        },
    )


def stable_threshold_decision(
    *,
    stage: str,
    value: float | None,
    threshold: float,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    evaluation = config["evaluation"]
    strategy = str(evaluation["threshold_strategy"])
    tolerance = float(evaluation["threshold_tolerance"])
    floor = float(evaluation["subnormal_floor"])
    boundary = str(evaluation["boundary_decision"])
    if value is None:
        return {
            "stage": stage,
            "applicable": False,
            "strategy": strategy,
            "threshold": float(threshold),
            "tolerance": tolerance,
            "subnormal_floor": floor,
            "decision": None,
            "decision_text": "not_applicable",
            "comparator": "none",
            "stability_guaranteed": True,
            "trace": ["metric is not defined for this method"],
        }

    raw_value = float(value)
    raw_threshold = float(threshold)
    value_subnormal = abs(raw_value) < floor
    threshold_subnormal = abs(raw_threshold) < floor
    subnormal_guarded = value_subnormal or threshold_subnormal
    protected_value = 0.0 if value_subnormal else raw_value
    protected_threshold = 0.0 if threshold_subnormal else raw_threshold
    guard_scale = max(abs(raw_value), abs(raw_threshold), 1.0)
    effective_tolerance = tolerance
    if strategy == "tolerance_band":
        effective_tolerance = max(tolerance, floor * guard_scale)
    delta = protected_value - protected_threshold

    if strategy == "strict":
        decision = bool(protected_value > protected_threshold)
        comparator = "value > threshold"
        stable = False
        trace = [
            "strict comparison does not absorb values that straddle the threshold",
            "subnormal values are canonicalized to zero before comparison",
        ]
    elif strategy == "tolerance_band":
        if delta > effective_tolerance:
            decision = True
            comparator = "value > threshold + tolerance"
        elif delta < -effective_tolerance:
            decision = False
            comparator = "value < threshold - tolerance"
        else:
            decision = bool(boundary == "accept")
            comparator = f"|value - threshold| <= tolerance; boundary={boundary}"
        stable = True
        trace = [
            "values inside the tolerance band use a deterministic boundary decision",
            "subnormal guard is included in the effective tolerance",
        ]
    else:
        raise ValueError(f"unsupported threshold strategy: {strategy}")

    return {
        "stage": stage,
        "applicable": True,
        "value": raw_value,
        "threshold": float(threshold),
        "strategy": strategy,
        "tolerance": tolerance,
        "effective_tolerance": float(effective_tolerance),
        "subnormal_floor": floor,
        "subnormal_guarded": bool(subnormal_guarded),
        "value_subnormal_guarded": bool(value_subnormal),
        "threshold_subnormal_guarded": bool(threshold_subnormal),
        "protected_value": float(protected_value),
        "protected_threshold": float(protected_threshold),
        "delta": float(delta),
        "decision": bool(decision),
        "decision_text": "accept" if decision else "reject",
        "comparator": comparator,
        "stability_guaranteed": stable,
        "trace": trace,
    }


def threshold_trace(
    result: ExecutionResult, config: Mapping[str, Any]
) -> dict[str, Any]:
    evaluation = config["evaluation"]
    class_trace = stable_threshold_decision(
        stage="class_consistency",
        value=result.class_consistency,
        threshold=float(evaluation["class_consistency_threshold"]),
        config=config,
    )
    variance_trace = stable_threshold_decision(
        stage="variance_explained",
        value=result.variance_explained,
        threshold=float(evaluation["variance_explained_threshold"]),
        config=config,
    )
    class_trace["metric"] = result.class_consistency
    variance_trace["metric"] = result.variance_explained
    class_trace["decision"] = class_trace["decision"]
    variance_trace["decision"] = variance_trace["decision"]
    return {
        "strategy": evaluation["threshold_strategy"],
        "tolerance": evaluation["threshold_tolerance"],
        "subnormal_floor": evaluation["subnormal_floor"],
        "boundary_decision": evaluation["boundary_decision"],
        "decisions": [class_trace, variance_trace],
    }


def _finite_or_none(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
