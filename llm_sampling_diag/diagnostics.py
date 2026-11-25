from __future__ import annotations

from typing import Any

from .errors import DiagnosticError, ErrorCode
from .model import OperatorConfig, UnifiedTask
from .operators import execute_sampling
from .validation import validate_config, validate_task


def _candidate_map(result: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(candidate["index"]): candidate for candidate in result["candidates"]}


def _first_sample_mismatch(
    reference: list[int],
    actual: list[int],
) -> dict[str, Any] | None:
    for position, (expected, observed) in enumerate(zip(reference, actual)):
        if expected != observed:
            return {
                "position": position,
                "reference_sample": expected,
                "actual_sample": observed,
                "candidate_index": observed,
            }
    if len(reference) != len(actual):
        return {
            "position": min(len(reference), len(actual)),
            "reference_length": len(reference),
            "actual_length": len(actual),
            "candidate_index": actual[min(len(reference), len(actual))] if actual else None,
        }
    return None


def diagnose(task: UnifiedTask, config: OperatorConfig) -> dict[str, Any]:
    validate_config(config)
    validate_task(task)
    reference_config = OperatorConfig(
        mask_priority=config.mask_priority,
        dtype="float64",
        rounding_mode="none",
        subnormal_guard="none",
        tolerance=config.tolerance,
        rng_algorithm=config.rng_algorithm,
    )
    reference = execute_sampling(
        task,
        reference_config,
        dtype_override="float64",
        rounding_override="none",
        subnormal_guard_override="none",
    ).to_dict()
    actual = execute_sampling(
        task,
        config,
        dtype_override=config.dtype,
        rounding_override=config.rounding_mode,
        subnormal_guard_override=config.subnormal_guard,
    ).to_dict()
    reference_map = _candidate_map(reference)
    actual_map = _candidate_map(actual)
    all_indices = sorted(set(reference_map) | set(actual_map))
    score_errors: list[dict[str, Any]] = []
    probability_errors: list[dict[str, Any]] = []
    for index in all_indices:
        expected = reference_map.get(index)
        observed = actual_map.get(index)
        if expected is None or observed is None:
            score_errors.append(
                {
                    "index": index,
                    "reference": None if expected is None else expected["score"],
                    "actual": None if observed is None else observed["score"],
                    "abs_error": None,
                    "present_in_reference": expected is not None,
                    "present_in_actual": observed is not None,
                }
            )
            probability_errors.append(
                {
                    "index": index,
                    "reference": None if expected is None else expected["probability"],
                    "actual": None if observed is None else observed["probability"],
                    "abs_error": None,
                }
            )
            continue
        score_error = abs(float(observed["score"]) - float(expected["score"]))
        probability_error = abs(float(observed["probability"]) - float(expected["probability"]))
        score_errors.append(
            {
                "index": index,
                "reference": expected["score"],
                "actual": observed["score"],
                "abs_error": score_error,
            }
        )
        probability_errors.append(
            {
                "index": index,
                "reference": expected["probability"],
                "actual": observed["probability"],
                "abs_error": probability_error,
            }
        )
    numeric_score_errors = [item for item in score_errors if item["abs_error"] is not None]
    numeric_probability_errors = [
        item for item in probability_errors if item["abs_error"] is not None
    ]
    max_score_error = max(numeric_score_errors, key=lambda item: item["abs_error"], default=None)
    max_probability_error = max(
        numeric_probability_errors,
        key=lambda item: item["abs_error"],
        default=None,
    )
    max_abs_error = max(
        float(max_score_error["abs_error"]) if max_score_error is not None else 0.0,
        float(max_probability_error["abs_error"]) if max_probability_error is not None else 0.0,
    )
    effective_tolerance = min(float(config.tolerance), float(task.error_budget))
    candidate_order_equal = reference["candidate_order"] == actual["candidate_order"]
    sample_mismatch = _first_sample_mismatch(
        list(reference["sample_sequence"]),
        list(actual["sample_sequence"]),
    )
    spectrum = {
        "case_id": task.case_id,
        "parameter_version": task.revision,
        "reference_dtype": "float64",
        "actual_dtype": config.dtype,
        "rounding_mode": config.rounding_mode,
        "subnormal_guard": config.subnormal_guard,
        "effective_tolerance": effective_tolerance,
        "candidate_scores": score_errors,
        "candidate_probabilities": probability_errors,
        "candidate_order_equal": candidate_order_equal,
        "sample_sequence_equal": sample_mismatch is None,
        "first_sample_mismatch": sample_mismatch,
        "max_score_abs_error": float(max_score_error["abs_error"]) if max_score_error is not None else 0.0,
        "max_probability_abs_error": (
            float(max_probability_error["abs_error"]) if max_probability_error is not None else 0.0
        ),
        "max_abs_error": max_abs_error,
        "within_tolerance": candidate_order_equal
        and sample_mismatch is None
        and all(item["abs_error"] is not None for item in score_errors)
        and all(item["abs_error"] is not None for item in probability_errors)
        and max_abs_error <= effective_tolerance,
        "subnormal_events": actual["subnormal_events"],
        "reference_subnormal_events": reference["subnormal_events"],
        "rounding_trace": actual["rounding_trace"],
    }
    if not candidate_order_equal:
        first_position = 0
        for first_position, (expected, observed) in enumerate(
            zip(reference["candidate_order"], actual["candidate_order"])
        ):
            if expected != observed:
                break
        else:
            first_position = min(len(reference["candidate_order"]), len(actual["candidate_order"]))
        candidate_index = (
            actual["candidate_order"][first_position]
            if first_position < len(actual["candidate_order"])
            else score_errors[0]["index"]
        )
        raise DiagnosticError(
            ErrorCode.E_TOLERANCE_EXCEEDED,
            "candidate order differs from the float64 reference",
            case_id=task.case_id,
            candidate_index=candidate_index,
            stage="candidate_selection",
            details={
                "effective_tolerance": effective_tolerance,
                "first_mismatch_position": first_position,
                "reference_order": reference["candidate_order"],
                "actual_order": actual["candidate_order"],
                "error_spectrum": spectrum,
            },
        )
    if max_score_error is not None and float(max_score_error["abs_error"]) > effective_tolerance:
        raise DiagnosticError(
            ErrorCode.E_TOLERANCE_EXCEEDED,
            "candidate score deviation exceeds tolerance",
            case_id=task.case_id,
            candidate_index=int(max_score_error["index"]),
            stage="temperature_scaling",
            details={
                "abs_error": max_score_error["abs_error"],
                "effective_tolerance": effective_tolerance,
                "error_spectrum": spectrum,
            },
        )
    if max_probability_error is not None and float(max_probability_error["abs_error"]) > effective_tolerance:
        raise DiagnosticError(
            ErrorCode.E_TOLERANCE_EXCEEDED,
            "candidate probability deviation exceeds tolerance",
            case_id=task.case_id,
            candidate_index=int(max_probability_error["index"]),
            stage="top_p_or_sampling_distribution",
            details={
                "abs_error": max_probability_error["abs_error"],
                "effective_tolerance": effective_tolerance,
                "error_spectrum": spectrum,
            },
        )
    if sample_mismatch is not None:
        raise DiagnosticError(
            ErrorCode.E_TOLERANCE_EXCEEDED,
            "sampling sequence differs from the float64 reference",
            case_id=task.case_id,
            candidate_index=sample_mismatch.get("candidate_index"),
            stage="sampling",
            details={
                "effective_tolerance": effective_tolerance,
                "first_sample_mismatch": sample_mismatch,
                "error_spectrum": spectrum,
            },
        )
    return spectrum

