"""Frame-level quality metrics that can be independently recomputed."""

from __future__ import annotations

import math
from typing import Any, Iterable

from .canonical import canonical_bytes, r6, sha256_bytes
from .models import ClipSpec, FrameSample


def _center(box: Iterable[float]) -> tuple[float, float]:
    values = list(box)
    return ((values[0] + values[2]) / 2.0, (values[1] + values[3]) / 2.0)


def _iou(left: Iterable[float], right: Iterable[float]) -> float:
    a = list(left)
    b = list(right)
    x0 = max(a[0], b[0])
    y0 = max(a[1], b[1])
    x1 = min(a[2], b[2])
    y1 = min(a[3], b[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union > 0.0 else 0.0


def evaluate_quality(
    spec: ClipSpec,
    output_payload: dict[str, Any],
    truth_frames: Iterable[FrameSample],
    *,
    hit_iou_threshold: float = 0.35,
) -> dict[str, Any]:
    """Compute displacement error, trajectory hit rate and temporal consistency."""

    truth_map = {frame.frame_index: frame for frame in truth_frames}
    output_map = {int(frame["frame_index"]): frame for frame in output_payload.get("frames", [])}
    details: list[dict[str, Any]] = []
    displacement_errors: list[float] = []
    hits: list[int] = []
    total_objects = 0
    previous: dict[int, tuple[tuple[float, float], tuple[float, float]]] = {}
    consistency_errors: list[float] = []

    for frame_index in range(spec.frame_count):
        if frame_index not in output_map or frame_index not in truth_map:
            continue
        output_frame = output_map[frame_index]
        truth_frame = truth_map[frame_index]
        predicted = {int(item["object_id"]): item for item in output_frame.get("predictions", [])}
        truth = {target.object_id: target for target in truth_frame.targets}
        for object_id in range(spec.object_count):
            predicted_item = predicted.get(object_id)
            truth_item = truth.get(object_id)
            if predicted_item is None or truth_item is None:
                continue
            predicted_center = _center(predicted_item["box"])
            truth_center = _center(truth_item.box)
            iou = _iou(predicted_item["box"], truth_item.box)
            hit = int(iou >= hit_iou_threshold)
            total_objects += 1
            hits.append(hit)
            predicted_displacement = None
            truth_displacement = None
            if object_id in previous:
                previous_predicted_center, previous_truth_center = previous[object_id]
                predicted_displacement = (
                    predicted_center[0] - previous_predicted_center[0],
                    predicted_center[1] - previous_predicted_center[1],
                )
                truth_displacement = (
                    truth_center[0] - previous_truth_center[0],
                    truth_center[1] - previous_truth_center[1],
                )
                error = math.hypot(
                    predicted_displacement[0] - truth_displacement[0],
                    predicted_displacement[1] - truth_displacement[1],
                )
                displacement_errors.append(error)
            details.append(
                {
                    "frame_index": frame_index,
                    "object_id": object_id,
                    "predicted_box": [r6(value) for value in predicted_item["box"]],
                    "truth_box": [r6(value) for value in truth_item.box],
                    "predicted_center": [r6(predicted_center[0]), r6(predicted_center[1])],
                    "truth_center": [r6(truth_center[0]), r6(truth_center[1])],
                    "predicted_displacement": (
                        [r6(value) for value in predicted_displacement] if predicted_displacement is not None else None
                    ),
                    "truth_displacement": [r6(value) for value in truth_displacement] if truth_displacement is not None else None,
                    "displacement_error": (
                        r6(math.hypot(
                            predicted_displacement[0] - truth_displacement[0],
                            predicted_displacement[1] - truth_displacement[1],
                        ))
                        if predicted_displacement is not None and truth_displacement is not None
                        else None
                    ),
                    "iou": r6(iou),
                    "hit": bool(hit),
                }
            )
            previous[object_id] = (predicted_center, truth_center)

    for index in range(1, len(details)):
        current = details[index]
        prior = details[index - 1]
        if (
            current["object_id"] == prior["object_id"]
            and current["frame_index"] == prior["frame_index"] + 1
            and current["predicted_displacement"] is not None
            and prior["predicted_displacement"] is not None
            and current["truth_displacement"] is not None
            and prior["truth_displacement"] is not None
        ):
            predicted_acceleration = (
                current["predicted_displacement"][0] - prior["predicted_displacement"][0],
                current["predicted_displacement"][1] - prior["predicted_displacement"][1],
            )
            truth_acceleration = (
                current["truth_displacement"][0] - prior["truth_displacement"][0],
                current["truth_displacement"][1] - prior["truth_displacement"][1],
            )
            consistency_errors.append(
                math.hypot(
                    predicted_acceleration[0] - truth_acceleration[0],
                    predicted_acceleration[1] - truth_acceleration[1],
                )
            )

    mean_displacement_error = sum(displacement_errors) / len(displacement_errors) if displacement_errors else 0.0
    hit_rate = sum(hits) / total_objects if total_objects else 0.0
    mean_consistency_error = sum(consistency_errors) / len(consistency_errors) if consistency_errors else 0.0
    metrics = {
        "clip_id": spec.clip_id,
        "displacement_error_px": r6(mean_displacement_error),
        "trajectory_hit_rate": r6(hit_rate),
        "temporal_consistency": r6(1.0 / (1.0 + mean_consistency_error)),
        "temporal_consistency_error_px": r6(mean_consistency_error),
        "sample_counts": {
            "object_frames": total_objects,
            "displacement_samples": len(displacement_errors),
            "consistency_samples": len(consistency_errors),
        },
        "formulas": {
            "displacement_error_px": "mean(Euclidean(predicted_delta - truth_delta))",
            "trajectory_hit_rate": f"mean(IoU >= {r6(hit_iou_threshold)})",
            "temporal_consistency": "1 / (1 + mean(abs(predicted_delta2 - truth_delta2)))",
        },
        "hit_iou_threshold": r6(hit_iou_threshold),
        "details": details,
    }
    metrics["details_checksum"] = sha256_bytes(canonical_bytes(details))
    return metrics


def recompute_metrics_from_details(details: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Independent recomputation helper used by tests and validation entry points."""

    rows = list(details)
    displacement_errors = [
        float(row["displacement_error"])
        for row in rows
        if row.get("displacement_error") is not None
    ]
    hits = [1.0 if row.get("hit") else 0.0 for row in rows]
    consistency_errors: list[float] = []
    for index in range(1, len(rows)):
        current = rows[index]
        prior = rows[index - 1]
        if (
            current["object_id"] == prior["object_id"]
            and current["frame_index"] == prior["frame_index"] + 1
            and current.get("predicted_displacement") is not None
            and prior.get("predicted_displacement") is not None
            and current.get("truth_displacement") is not None
            and prior.get("truth_displacement") is not None
        ):
            predicted_acceleration = (
                current["predicted_displacement"][0] - prior["predicted_displacement"][0],
                current["predicted_displacement"][1] - prior["predicted_displacement"][1],
            )
            truth_acceleration = (
                current["truth_displacement"][0] - prior["truth_displacement"][0],
                current["truth_displacement"][1] - prior["truth_displacement"][1],
            )
            consistency_errors.append(
                math.hypot(
                    predicted_acceleration[0] - truth_acceleration[0],
                    predicted_acceleration[1] - truth_acceleration[1],
                )
            )
    mean_consistency_error = sum(consistency_errors) / len(consistency_errors) if consistency_errors else 0.0
    return {
        "displacement_error_px": r6(sum(displacement_errors) / len(displacement_errors) if displacement_errors else 0.0),
        "trajectory_hit_rate": r6(sum(hits) / len(hits) if hits else 0.0),
        "temporal_consistency": r6(1.0 / (1.0 + mean_consistency_error)),
        "temporal_consistency_error_px": r6(mean_consistency_error),
    }
