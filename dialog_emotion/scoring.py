"""Segment-level ridge regression, intervals, and dialog aggregation."""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any

import numpy as np

from .canonical import content_hash, quantize
from .errors import fail


def validate_interval(
    low: float,
    high: float,
    *,
    dialog_id: str | None = None,
    segment_index: int | None = None,
    dimension: str = "valence",
) -> None:
    if not math.isfinite(low) or not math.isfinite(high) or low > high:
        fail(
            "E_INTERVAL_INVALID",
            "confidence interval bounds are not ordered",
            dialog_id=dialog_id,
            segment_index=segment_index,
            dimension=dimension,
            low=low,
            high=high,
        )


def score_segment(
    dialog_id: str,
    segment: dict[str, Any],
    feature_data: dict[str, Any],
    truth_rows: list[dict[str, Any]],
    ridge_lambda: float,
    confidence_level: float,
) -> dict[str, Any]:
    start = int(segment["start_frame"])
    end = int(segment["end_frame"])
    matrix = np.asarray(feature_data["features"][start:end], dtype=np.float64)
    if matrix.shape[0] != end - start:
        fail(
            "E_SEGMENT_GAP",
            "segment feature slice is incomplete",
            dialog_id=dialog_id,
            segment_index=segment["segment_index"],
            expected_rows=end - start,
            actual_rows=matrix.shape[0],
        )
    if len(truth_rows) != end - start:
        fail(
            "E_FRAME_MISMATCH",
            "segment truth rows do not match segment frames",
            dialog_id=dialog_id,
            segment_index=segment["segment_index"],
            expected=end - start,
            actual=len(truth_rows),
        )
    y_valence = np.asarray([float(row["valence"]) for row in truth_rows])
    y_arousal = np.asarray([float(row["arousal"]) for row in truth_rows])
    design = _design_matrix(matrix)
    valence_score, valence_ci = _ridge_fit(
        design, y_valence, ridge_lambda, confidence_level
    )
    arousal_score, arousal_ci = _ridge_fit(
        design, y_arousal, ridge_lambda, confidence_level
    )
    validate_interval(
        valence_ci[0],
        valence_ci[1],
        dialog_id=dialog_id,
        segment_index=segment["segment_index"],
        dimension="valence",
    )
    validate_interval(
        arousal_ci[0],
        arousal_ci[1],
        dialog_id=dialog_id,
        segment_index=segment["segment_index"],
        dimension="arousal",
    )
    score_data = {
        "dialog_id": dialog_id,
        "segment_index": int(segment["segment_index"]),
        "start_frame": start,
        "end_frame": end,
        "start_ms": segment["start_ms"],
        "end_ms": segment["end_ms"],
        "duration_ms": segment["duration_ms"],
        "speaker_id": segment["speaker_id"],
        "regressor": "deterministic-ridge-intercept-v1",
        "ridge_lambda": ridge_lambda,
        "valence": valence_score,
        "arousal": arousal_score,
        "valence_ci": {
            "low": valence_ci[0],
            "high": valence_ci[1],
            "level": confidence_level,
        },
        "arousal_ci": {
            "low": arousal_ci[0],
            "high": arousal_ci[1],
            "level": confidence_level,
        },
        "truth_rows": [
            {
                "frame_index": int(row["frame_index"]),
                "speaker_id": row["speaker_id"],
                "valence": quantize(row["valence"]),
                "arousal": quantize(row["arousal"]),
                "is_speech": bool(row["is_speech"]),
                "time_ms": quantize(row.get("time_ms", 0.0)),
            }
            for row in truth_rows
        ],
    }
    score_data["score_hash"] = content_hash(
        {
            "valence": score_data["valence"],
            "arousal": score_data["arousal"],
            "valence_ci": score_data["valence_ci"],
            "arousal_ci": score_data["arousal_ci"],
        }
    )
    return score_data


def aggregate_dialog(
    dialog_id: str,
    segment_scores: list[dict[str, Any]],
    weight_basis: str,
) -> dict[str, Any]:
    if not segment_scores:
        fail(
            "E_SEGMENT_GAP",
            "dialog has no segment scores to aggregate",
            dialog_id=dialog_id,
        )
    sorted_scores = sorted(
        segment_scores, key=lambda item: int(item["segment_index"])
    )
    contributions: list[dict[str, Any]] = []
    total_weight = 0.0
    valence_sum = 0.0
    arousal_sum = 0.0
    for score in sorted_scores:
        if weight_basis == "segment_duration_ms":
            weight = float(score["duration_ms"])
        else:
            fail(
                "E_AGGREGATION_CONFIG_INVALID",
                "unsupported aggregation weight basis",
                dialog_id=dialog_id,
                weight_basis=weight_basis,
            )
        if weight <= 0.0:
            fail(
                "E_RESOURCE_LIMIT",
                "aggregation weight must be positive",
                dialog_id=dialog_id,
                segment_index=score["segment_index"],
                weight=weight,
            )
        total_weight += weight
        valence_sum += float(score["valence"]) * weight
        arousal_sum += float(score["arousal"]) * weight
        contributions.append(
            {
                "segment_index": int(score["segment_index"]),
                "weight": quantize(weight),
                "valence": score["valence"],
                "arousal": score["arousal"],
                "valence_contribution": quantize(float(score["valence"]) * weight),
                "arousal_contribution": quantize(float(score["arousal"]) * weight),
            }
        )
    if total_weight <= 0.0:
        fail(
            "E_RESOURCE_LIMIT",
            "aggregation total weight must be positive",
            dialog_id=dialog_id,
        )
    result = {
        "dialog_id": dialog_id,
        "weight_basis": weight_basis,
        "total_weight": quantize(total_weight),
        "valence": quantize(valence_sum / total_weight),
        "arousal": quantize(arousal_sum / total_weight),
        "segment_count": len(sorted_scores),
        "contributions": contributions,
    }
    recomputed = recompute_aggregate(result)
    if (
        abs(recomputed["valence"] - result["valence"]) > 1e-12
        or abs(recomputed["arousal"] - result["arousal"]) > 1e-12
    ):
        fail(
            "E_INCREMENTAL_DRIFT",
            "dialog aggregate cannot be independently reproduced",
            dialog_id=dialog_id,
            first_divergent_node="dialog_aggregate",
        )
    result["independent_recompute"] = {
        **recomputed,
        "matches": True,
    }
    return result


def recompute_aggregate(aggregate: dict[str, Any]) -> dict[str, float]:
    total_weight = sum(float(item["weight"]) for item in aggregate["contributions"])
    valence = (
        sum(
            float(item["valence"]) * float(item["weight"])
            for item in aggregate["contributions"]
        )
        / total_weight
    )
    arousal = (
        sum(
            float(item["arousal"]) * float(item["weight"])
            for item in aggregate["contributions"]
        )
        / total_weight
    )
    return {
        "total_weight": quantize(total_weight),
        "valence": quantize(valence),
        "arousal": quantize(arousal),
    }


def _design_matrix(matrix: np.ndarray) -> np.ndarray:
    features = np.asarray(matrix, dtype=np.float64)
    if features.ndim != 2 or features.shape[1] != 4:
        fail(
            "E_FEATURE_INVALID",
            "segment regression expects four feature columns",
            shape=list(features.shape),
        )
    mean = np.mean(features, axis=0)
    scale = np.std(features, axis=0)
    scale = np.where(scale > 1e-12, scale, 1.0)
    standardized = (features - mean) / scale
    return np.column_stack([np.ones(len(features)), standardized])


def _ridge_fit(
    design: np.ndarray,
    target: np.ndarray,
    ridge_lambda: float,
    confidence_level: float,
) -> tuple[float, tuple[float, float]]:
    penalty = np.eye(design.shape[1], dtype=np.float64) * ridge_lambda
    penalty[0, 0] = 1e-9
    system = design.T @ design + penalty
    try:
        coefficients = np.linalg.solve(system, design.T @ target)
    except np.linalg.LinAlgError:
        coefficients = np.linalg.pinv(system) @ design.T @ target
    fitted = design @ coefficients
    score = float(np.mean(fitted))
    residual = target - fitted
    degrees = max(1, len(target) - 1)
    residual_std = float(np.sqrt(np.sum(residual * residual) / degrees))
    z_value = NormalDist().inv_cdf(0.5 + confidence_level / 2.0)
    half_width = z_value * residual_std / math.sqrt(max(1, len(target)))
    low = max(0.0, score - half_width)
    high = min(1.0, score + half_width)
    return quantize(score), (quantize(low), quantize(high))
