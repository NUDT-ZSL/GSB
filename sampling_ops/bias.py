"""Float64 full-sort reference adjudication and replay-based self checks."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

import numpy as np

from .errors import fail
from .sampling import sample
from .topk import TopKResult, first_deviation, topk


REFERENCE_ROUTE = "full_sort_float64_reference"


def _result_parts(value: Any) -> tuple[np.ndarray, np.ndarray]:
    if isinstance(value, TopKResult):
        return np.asarray(value.indices), np.asarray(value.scores)
    if isinstance(value, Mapping):
        return np.asarray(value["indices"]), np.asarray(value["scores"])
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return np.asarray(value[0]), np.asarray(value[1])
    raise TypeError(f"unsupported candidate result type: {type(value).__name__}")


def _sample_array(value: Any) -> np.ndarray:
    if hasattr(value, "samples"):
        return np.asarray(value.samples)
    if isinstance(value, Mapping) and "samples" in value:
        return np.asarray(value["samples"])
    return np.asarray(value)


def _first_sample_mismatch(actual: np.ndarray, expected: np.ndarray) -> dict[str, Any] | None:
    if actual.shape != expected.shape:
        return {
            "row": 0,
            "position": 0,
            "actual_shape": list(actual.shape),
            "expected_shape": list(expected.shape),
        }
    difference = actual.astype(np.int64) != expected.astype(np.int64)
    if not np.any(difference):
        return None
    row, position = np.argwhere(difference)[0].tolist()
    return {
        "row": int(row),
        "position": int(position),
        "actual": int(actual[row, position]),
        "expected": int(expected[row, position]),
    }


def _candidate_hash(indices: np.ndarray) -> str:
    return hashlib.sha256(np.asarray(indices, dtype="<i8").tobytes(order="C")).hexdigest()


def _validate_tolerance(value: Any, *, case_id: str | None) -> float:
    try:
        tolerance = float(value)
    except (TypeError, ValueError, OverflowError):
        fail(
            "E_TOLERANCE_INVALID",
            "tolerance must be a finite non-negative number",
            field="tolerance",
            case_id=case_id,
        )
    if not np.isfinite(tolerance) or tolerance < 0:
        fail(
            "E_TOLERANCE_INVALID",
            "tolerance must be a finite non-negative number",
            field="tolerance",
            case_id=case_id,
            details={"actual": value},
        )
    return tolerance


def adjudicate_bias(
    case_id: str,
    logits: Any,
    k_value: int,
    route_candidates: Mapping[str, Any],
    *,
    temperature: Any,
    top_p: Any,
    seed: Any,
    group_size: Any,
    route_samples: Mapping[str, Any] | None = None,
    tolerance: Any = 1e-12,
    chunk_size: int = 4096,
) -> dict[str, Any]:
    """Compare every route against a float64 full-sort reference.

    The reference uses the same tie policy.  Index differences are never
    hidden by score tolerance: any index, score, or sample mismatch produces
    ``E_TOLERANCE_EXCEEDED`` with the first offending element.
    """

    tolerance_value = _validate_tolerance(tolerance, case_id=case_id)
    reference = topk(
        np.asarray(logits, dtype=np.float64),
        int(k_value),
        route="full_sort",
        tie_policy="index_asc",
        case_id=case_id,
    )
    reference_samples = sample(
        reference.scores,
        reference.indices,
        temperature=temperature,
        top_p=top_p,
        seed=seed,
        group_size=group_size,
        case_id=case_id,
    )

    route_reports: dict[str, Any] = {}
    violations: list[dict[str, Any]] = []
    all_indices_equal = True
    all_samples_equal = True
    sample_checks_performed = 0
    overall_max_score = 0.0
    overall_max_sample = 0

    for route in ("full_sort", "heap_select", "chunked_two_stage"):
        if route not in route_candidates:
            all_indices_equal = False
            violations.append(
                {
                    "case_id": case_id,
                    "route": route,
                    "business_code": "E_TOLERANCE_EXCEEDED",
                    "kind": "missing_route",
                    "element": f"{route}.candidates",
                }
            )
            continue
        indices, scores = _result_parts(route_candidates[route])
        indices = indices.astype(np.int64, copy=False)
        scores = scores.astype(np.float64, copy=False)
        deviation = first_deviation(indices, reference.indices)
        scores_shape_ok = scores.shape == reference.scores.shape
        if scores_shape_ok:
            score_deviations = np.abs(scores - reference.scores.astype(np.float64))
            max_score = float(np.max(score_deviations)) if score_deviations.size else 0.0
            max_position = (
                tuple(int(item) for item in np.unravel_index(np.argmax(score_deviations), score_deviations.shape))
                if score_deviations.size
                else (0, 0)
            )
        else:
            score_deviations = np.asarray([])
            max_score = float("inf")
            max_position = (0, 0)
        overall_max_score = max(overall_max_score, max_score)

        if deviation is not None:
            all_indices_equal = False
            violations.append(
                {
                    "case_id": case_id,
                    "route": route,
                    "business_code": "E_TOLERANCE_EXCEEDED",
                    "kind": "candidate_indices",
                    "element": (
                        f"batch[{deviation.get('row', 0)}].candidate[{deviation.get('position', 0)}]"
                    ),
                    "actual": deviation.get("actual"),
                    "expected": deviation.get("expected"),
                }
            )
        if not scores_shape_ok:
            violations.append(
                {
                    "case_id": case_id,
                    "route": route,
                    "business_code": "E_TOLERANCE_EXCEEDED",
                    "kind": "candidate_score_shape",
                    "element": f"{route}.candidate_scores",
                    "actual_shape": list(scores.shape),
                    "expected_shape": list(reference.scores.shape),
                }
            )
        elif max_score > tolerance_value:
            violations.append(
                {
                    "case_id": case_id,
                    "route": route,
                    "business_code": "E_TOLERANCE_EXCEEDED",
                    "kind": "candidate_score",
                    "element": f"batch[{max_position[0]}].candidate[{max_position[1]}]",
                    "actual": float(scores[max_position]),
                    "expected": float(reference.scores[max_position]),
                    "deviation": max_score,
                    "tolerance": tolerance_value,
                }
            )

        sample_report: dict[str, Any] = {"present": False}
        max_sample = 0
        if route_samples is not None and route in route_samples:
            sample_checks_performed += 1
            actual_samples = _sample_array(route_samples[route]).astype(np.int64, copy=False)
            expected_samples = reference_samples.samples.astype(np.int64, copy=False)
            mismatch = _first_sample_mismatch(actual_samples, expected_samples)
            sample_report = {
                "present": True,
                "consistent": mismatch is None,
                "first_mismatch": mismatch,
                "max_abs_sample_deviation": (
                    int(np.max(np.abs(actual_samples - expected_samples)))
                    if actual_samples.shape == expected_samples.shape and actual_samples.size
                    else None
                ),
            }
            if actual_samples.shape == expected_samples.shape and actual_samples.size:
                max_sample = int(np.max(np.abs(actual_samples - expected_samples)))
            if mismatch is not None:
                all_samples_equal = False
                violations.append(
                    {
                        "case_id": case_id,
                        "route": route,
                        "business_code": "E_TOLERANCE_EXCEEDED",
                        "kind": "sample_sequence",
                        "element": (
                            f"batch[{mismatch.get('row', 0)}].step[{mismatch.get('position', 0)}]"
                        ),
                        **mismatch,
                    }
                )
        overall_max_sample = max(overall_max_sample, max_sample)
        route_reports[route] = {
            "candidate_indices_equal": deviation is None,
            "candidate_score_shape_equal": scores_shape_ok,
            "max_abs_score_deviation": max_score,
            "candidate_score_deviations": score_deviations.tolist(),
            "sample": sample_report,
        }

    score_ok = overall_max_score <= tolerance_value
    samples_checked = route_samples is not None
    samples_ok = all_samples_equal if samples_checked else None
    ok = all_indices_equal and score_ok and (samples_ok is not False) and not violations
    return {
        "schema_version": "1.0",
        "case_id": case_id,
        "reference_route": REFERENCE_ROUTE,
        "reference_tie_policy": "score_desc_index_asc",
        "tolerance": tolerance_value,
        "reference_candidate_indices": reference.indices.tolist(),
        "reference_candidate_scores": [float(value) for value in reference.scores.ravel()],
        "reference_samples": reference_samples.samples.tolist(),
        "route_reports": route_reports,
        "all_candidate_indices_equal": bool(all_indices_equal),
        "all_sample_sequences_equal": bool(all_samples_equal) if samples_checked else None,
        "sample_checks_performed": int(sample_checks_performed),
        "sample_checks_expected": 3 if samples_checked else 0,
        "all_scores_within_tolerance": bool(score_ok),
        "max_abs_score_deviation": float(overall_max_score),
        "max_abs_sample_deviation": int(overall_max_sample),
        "violations": violations,
        "status": "OK" if ok else "E_TOLERANCE_EXCEEDED",
        "business_code": "OK" if ok else "E_TOLERANCE_EXCEEDED",
    }


def self_check(
    case_id: str,
    route_candidates: Mapping[str, Any],
    route_samples: Mapping[str, Any],
    *,
    temperature: Any,
    top_p: Any,
    seed: Any,
    group_size: Any,
    tolerance: Any = 1e-12,
) -> dict[str, Any]:
    """Check cross-route candidates and replay every sample sequence."""

    tolerance_value = _validate_tolerance(tolerance, case_id=case_id)
    baseline_indices: np.ndarray | None = None
    baseline_scores: np.ndarray | None = None
    failures: list[dict[str, Any]] = []
    candidate_hashes: dict[str, str] = {}
    replay_reports: dict[str, Any] = {}

    for route in ("full_sort", "heap_select", "chunked_two_stage"):
        if route not in route_candidates:
            failures.append({"kind": "missing_route", "route": route, "element": "candidates"})
            continue
        indices, scores = _result_parts(route_candidates[route])
        indices = indices.astype(np.int64, copy=False)
        scores = scores.astype(np.float64, copy=False)
        candidate_hashes[route] = _candidate_hash(indices)
        if baseline_indices is None:
            baseline_indices = indices
            baseline_scores = scores
        else:
            deviation = first_deviation(indices, baseline_indices)
            if deviation is not None:
                failures.append(
                    {
                        "kind": "cross_route_candidate_mismatch",
                        "route": route,
                        "baseline_route": "full_sort",
                        "element": (
                            f"batch[{deviation.get('row', 0)}].candidate[{deviation.get('position', 0)}]"
                        ),
                        **deviation,
                    }
                )
            if scores.shape != baseline_scores.shape:
                failures.append(
                    {
                        "kind": "cross_route_score_shape_mismatch",
                        "route": route,
                        "element": f"{route}.candidate_scores",
                    }
                )
            else:
                max_deviation = float(np.max(np.abs(scores - baseline_scores))) if scores.size else 0.0
                if max_deviation > tolerance_value:
                    position = tuple(int(item) for item in np.unravel_index(np.argmax(np.abs(scores - baseline_scores)), scores.shape))
                    failures.append(
                        {
                            "kind": "cross_route_score_tolerance",
                            "route": route,
                            "element": f"batch[{position[0]}].candidate[{position[1]}]",
                            "deviation": max_deviation,
                            "tolerance": tolerance_value,
                        }
                    )

        if route not in route_samples:
            failures.append({"kind": "missing_route_samples", "route": route, "element": "samples"})
            continue
        stored = _sample_array(route_samples[route]).astype(np.int64, copy=False)
        replay = sample(
            scores,
            indices,
            temperature=temperature,
            top_p=top_p,
            seed=seed,
            group_size=group_size,
            case_id=case_id,
        )
        mismatch = _first_sample_mismatch(replay.samples, stored)
        replay_reports[route] = {
            "replay_equal": mismatch is None,
            "first_mismatch": mismatch,
            "candidate_hash": candidate_hashes[route],
        }
        if mismatch is not None:
            failures.append(
                {
                    "kind": "sample_replay_mismatch",
                    "route": route,
                    "element": (
                        f"batch[{mismatch.get('row', 0)}].step[{mismatch.get('position', 0)}]"
                    ),
                    **mismatch,
                }
            )

    if len(set(candidate_hashes.values())) > 1:
        failures.append({"kind": "candidate_hash_mismatch", "routes": sorted(candidate_hashes)})

    baseline_samples: np.ndarray | None = None
    for route, value in route_samples.items():
        current = _sample_array(value).astype(np.int64, copy=False)
        if baseline_samples is None:
            baseline_samples = current
        else:
            mismatch = _first_sample_mismatch(current, baseline_samples)
            if mismatch is not None:
                failures.append(
                    {
                        "kind": "cross_route_sample_mismatch",
                        "route": route,
                        "element": (
                            f"batch[{mismatch.get('row', 0)}].step[{mismatch.get('position', 0)}]"
                        ),
                        **mismatch,
                    }
                )

    ok = not failures
    return {
        "schema_version": "1.0",
        "case_id": case_id,
        "status": "OK" if ok else "E_INCONSISTENT",
        "business_code": "OK" if ok else "E_INCONSISTENT",
        "candidate_hashes": candidate_hashes,
        "all_candidate_hashes_equal": len(set(candidate_hashes.values())) <= 1,
        "replay": replay_reports,
        "all_samples_replay_equal": all(
            report["replay_equal"] for report in replay_reports.values()
        )
        if replay_reports
        else False,
        "failures": failures,
        "first_failure": failures[0] if failures else None,
    }

