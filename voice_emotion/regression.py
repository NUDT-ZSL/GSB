"""Stage 3 and 4: deterministic segment regression and dialogue aggregation."""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

import numpy as np

from .canonical import hash_value
from .config import AggregationConfig, RegressionConfig
from .errors import E_INTERVAL_INVALID, E_INPUT_INVALID, raise_error
from .speaker import Segment, embedding_vectors


TARGETS = ("valence", "arousal")


def validate_interval(
    low: float,
    high: float,
    *,
    context: str = "interval",
) -> None:
    if not math.isfinite(float(low)) or not math.isfinite(float(high)):
        raise_error(
            E_INTERVAL_INVALID,
            f"{context} bounds must be finite",
            ci_low=low,
            ci_high=high,
        )
    if float(low) > float(high):
        raise_error(
            E_INTERVAL_INVALID,
            f"{context} lower bound exceeds upper bound",
            ci_low=low,
            ci_high=high,
        )


def _ridge_fit(
    design: np.ndarray,
    target: np.ndarray,
    ridge_lambda: float,
) -> tuple[np.ndarray, float, np.ndarray, np.ndarray]:
    x = np.asarray(design, dtype=np.float64)
    y = np.asarray(target, dtype=np.float64)
    x_mean = np.mean(x, axis=0, dtype=np.float64)
    y_mean = float(np.mean(y, dtype=np.float64))
    centered = x - x_mean
    centered_y = y - y_mean
    gram = centered.T @ centered
    if ridge_lambda > 0.0:
        gram = gram + float(ridge_lambda) * np.eye(gram.shape[0], dtype=np.float64)
    rhs = centered.T @ centered_y
    try:
        weights = np.linalg.solve(gram, rhs)
        inverse = np.linalg.inv(gram)
    except np.linalg.LinAlgError:
        weights, *_ = np.linalg.lstsq(gram, rhs, rcond=None)
        inverse = np.linalg.pinv(gram)
    predictions = centered @ weights + y_mean
    leverage = np.sum((centered @ inverse) * centered, axis=1, dtype=np.float64)
    denominator = np.maximum(1.0 - leverage, 1e-9)
    leave_one_out = y - (y - predictions) / denominator
    return (
        np.ascontiguousarray(weights, dtype=np.float64),
        y_mean,
        np.ascontiguousarray(predictions, dtype=np.float64),
        np.ascontiguousarray(leave_one_out, dtype=np.float64),
    )


def regress_segment(
    features: np.ndarray,
    truth_rows: Sequence[dict[str, Any]],
    segment: Segment | dict[str, Any],
    config: RegressionConfig,
    frame_shift_ms: float,
    truth_slice_hash: str,
) -> dict[str, Any]:
    config.validate()
    if isinstance(segment, Segment):
        start = segment.start_frame
        end = segment.end_frame
        segment_id = segment.segment_id
        speaker_id = segment.speaker_id
        start_sample = segment.start_sample
        end_sample = segment.end_sample
    else:
        start = int(segment["start_frame"])
        end = int(segment["end_frame"])
        segment_id = str(segment["segment_id"])
        speaker_id = int(segment["speaker_id"])
        start_sample = int(segment["start_sample"])
        end_sample = int(segment["end_sample"])
    frame_count = end - start
    if frame_count <= 0:
        raise_error(E_INPUT_INVALID, "segment has no frames", segment_id=segment_id)
    if len(truth_rows) != frame_count:
        raise_error(
            E_INPUT_INVALID,
            "truth slice does not cover the segment",
            segment_id=segment_id,
            expected=frame_count,
            actual=len(truth_rows),
        )
    matrix = np.asarray(features[start:end], dtype=np.float64)
    design = embedding_vectors(matrix)
    detail: dict[str, Any] = {
        "segment_id": segment_id,
        "start_frame": start,
        "end_frame": end,
        "speaker_id": speaker_id,
        "start_sample": start_sample,
        "end_sample": end_sample,
        "frame_count": frame_count,
        "duration_ms": float(frame_count) * float(frame_shift_ms),
        "weight_ms": float(frame_count) * float(frame_shift_ms),
        "truth_slice_hash": truth_slice_hash,
        "regression_config_hash": config.params_hash(),
    }
    for target_name in TARGETS:
        target = np.asarray([float(row[target_name]) for row in truth_rows], dtype=np.float64)
        weights, intercept, predictions, leave_one_out = _ridge_fit(
            design,
            target,
            config.ridge_lambda,
        )
        if config.prediction_scheme == "leave_one_out":
            emitted = np.clip(leave_one_out, -1.0, 1.0)
        else:
            emitted = np.clip(predictions, -1.0, 1.0)
        score = float(np.mean(emitted, dtype=np.float64))
        residuals = emitted - score
        standard_error = math.sqrt(
            max(0.0, float(np.dot(residuals, residuals)))
            / float(max(1, frame_count - 1))
            / float(frame_count)
        )
        margin = float(config.confidence_z) * standard_error
        score = float(np.clip(score, -1.0, 1.0))
        ci_low = float(np.clip(score - margin, -1.0, 1.0))
        ci_high = float(np.clip(score + margin, -1.0, 1.0))
        validate_interval(ci_low, ci_high, context=f"{segment_id}:{target_name}")
        detail[target_name] = {
            "score": score,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "standard_error": standard_error,
            "coefficients": [float(item) for item in weights.tolist()],
            "intercept": float(intercept),
            "confidence_z": float(config.confidence_z),
            "ridge_lambda": float(config.ridge_lambda),
            "prediction_scheme": config.prediction_scheme,
        }
    detail["output_hash"] = hash_value({"kind": "segment-score", **detail})
    return detail


def _segment_value(detail: dict[str, Any], target_name: str) -> dict[str, Any]:
    value = detail.get(target_name)
    if not isinstance(value, dict):
        raise_error(
            E_INTERVAL_INVALID,
            "segment detail is missing an emotion interval",
            segment_id=detail.get("segment_id"),
            target=target_name,
        )
    for field in ("score", "ci_low", "ci_high", "standard_error"):
        if field not in value:
            raise_error(
                E_INTERVAL_INVALID,
                "segment detail is missing interval fields",
                segment_id=detail.get("segment_id"),
                target=target_name,
                field=field,
            )
    validate_interval(
        float(value["ci_low"]),
        float(value["ci_high"]),
        context=f"{detail.get('segment_id')}:{target_name}",
    )
    return value


def aggregate_segments(
    segments: Iterable[dict[str, Any]],
    config: AggregationConfig,
) -> dict[str, Any]:
    """Aggregate segment estimates using explicit, auditable weights."""

    config.validate()
    details = sorted(list(segments), key=lambda item: (int(item["start_frame"]), str(item["segment_id"])))
    if not details:
        raise_error(E_INPUT_INVALID, "cannot aggregate an empty segment list")
    weights: list[float] = []
    weight_table: list[dict[str, Any]] = []
    for detail in details:
        segment_id = str(detail["segment_id"])
        if config.weight_basis == "segment_duration_ms":
            weight = float(detail["weight_ms"])
        else:
            weight = float(detail["frame_count"])
        if not math.isfinite(weight) or weight <= 0.0:
            raise_error(
                E_INPUT_INVALID,
                "segment weight must be finite and positive",
                segment_id=segment_id,
                weight=weight,
            )
        weights.append(weight)
        weight_table.append({"segment_id": segment_id, "weight": weight})
    total_weight = math.fsum(weights)
    if total_weight <= 0.0:
        raise_error(E_INPUT_INVALID, "total aggregation weight is not positive")
    scores: dict[str, Any] = {}
    for target_name in TARGETS:
        values = [_segment_value(detail, target_name) for detail in details]
        score = math.fsum(
            weight * float(value["score"])
            for weight, value in zip(weights, values)
        ) / total_weight
        variance = math.fsum(
            ((weight / total_weight) ** 2) * (float(value["standard_error"]) ** 2)
            for weight, value in zip(weights, values)
        )
        standard_error = math.sqrt(max(0.0, variance))
        margin = float(config.confidence_z) * standard_error
        ci_low = float(np.clip(score - margin, -1.0, 1.0))
        ci_high = float(np.clip(score + margin, -1.0, 1.0))
        validate_interval(ci_low, ci_high, context=f"dialog:{target_name}")
        scores[target_name] = {
            "score": float(score),
            "ci_low": ci_low,
            "ci_high": ci_high,
            "standard_error": standard_error,
        }
    aggregation = {
        "weight_basis": config.weight_basis,
        "formula": "sum(segment_score_i * weight_i) / sum(weight_i)",
        "interval_method": "sqrt(sum((weight_i / total_weight)^2 * segment_se_i^2))",
        "confidence_z": float(config.confidence_z),
        "weights": weight_table,
        "total_weight": total_weight,
        "segment_count": len(details),
        "aggregation_config_hash": config.params_hash(),
    }
    body = {
        "kind": "dialog-aggregate",
        "scores": scores,
        "aggregation": aggregation,
        "segments": details,
    }
    return {**body, "output_hash": hash_value(body)}
