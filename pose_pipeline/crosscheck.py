from __future__ import annotations

import math
from typing import Any

from .errors import PipelineError
from .heatmap import selected_peaks_by_channel
from .utils import bbox_iou, round_float


def _instance_points(instance: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"x": point["x"], "y": point["y"]}
        for point in instance["keypoints"]
    ]


def compare_paths(
    exact: dict[str, Any],
    approx: dict[str, Any],
    sample: dict[str, Any],
    tolerance_px: float,
) -> dict[str, Any]:
    exact_peaks = selected_peaks_by_channel(exact["decoded"])
    approx_peaks = selected_peaks_by_channel(approx["decoded"])
    expected_channels = set(range(sample["num_channels"]))
    if set(exact_peaks) != expected_channels or set(approx_peaks) != expected_channels:
        raise PipelineError(
            "E_INCONSISTENT",
            "cross_path.channels",
            "exact and approximate paths must cover every heatmap channel",
            image_id=sample.get("image_id"),
            category="CROSS_PATH_CHANNEL_COVERAGE",
        )

    coordinate_differences = []
    for channel in sorted(expected_channels):
        exact_peak = exact_peaks[channel]
        approx_peak = approx_peaks[channel]
        delta_x = float(approx_peak["x"]) - float(exact_peak["x"])
        delta_y = float(approx_peak["y"]) - float(exact_peak["y"])
        delta = math.hypot(delta_x, delta_y)
        coordinate_differences.append(
            {
                "channel_index": channel,
                "keypoint_index": exact_peak["keypoint_index"],
                "exact_peak_id": exact_peak["peak_id"],
                "approx_peak_id": approx_peak["peak_id"],
                "exact_position": {"x": exact_peak["x"], "y": exact_peak["y"]},
                "approx_position": {"x": approx_peak["x"], "y": approx_peak["y"]},
                "delta_x": round_float(delta_x),
                "delta_y": round_float(delta_y),
                "distance_px": round_float(delta),
            }
        )
    violations = [
        item
        for item in coordinate_differences
        if item["distance_px"] > float(tolerance_px)
    ]

    exact_instances = {
        instance["instance_id"]: instance for instance in exact["grouped"]["instances"]
    }
    approx_instances = {
        instance["instance_id"]: instance
        for instance in approx["grouped"]["instances"]
    }
    instance_iou = []
    for instance_id in sorted(set(exact_instances) & set(approx_instances)):
        iou = bbox_iou(
            _instance_points(exact_instances[instance_id]),
            _instance_points(approx_instances[instance_id]),
        )
        instance_iou.append(
            {
                "instance_id": instance_id,
                "bbox_iou": iou,
            }
        )

    exact_per_keypoint = exact["metrics"]["per_keypoint"]
    approx_per_keypoint = approx["metrics"]["per_keypoint"]
    pck_delta_spectrum = []
    for key in sorted(
        set(exact_per_keypoint) | set(approx_per_keypoint),
        key=lambda value: int(value),
    ):
        exact_value = exact_per_keypoint.get(
            key,
            {
                "pck": 0.0,
                "pck_hits": 0,
                "evaluated_keypoints": 0,
            },
        )
        approx_value = approx_per_keypoint.get(
            key,
            {
                "pck": 0.0,
                "pck_hits": 0,
                "evaluated_keypoints": 0,
            },
        )
        pck_delta_spectrum.append(
            {
                "keypoint_index": int(key),
                "exact_pck": exact_value["pck"],
                "approx_pck": approx_value["pck"],
                "delta_pck_exact_minus_approx": round_float(
                    float(exact_value["pck"]) - float(approx_value["pck"])
                ),
                "exact_hits": exact_value["pck_hits"],
                "approx_hits": approx_value["pck_hits"],
                "evaluated_keypoints": exact_value["evaluated_keypoints"],
            }
        )

    max_coordinate_delta = max(
        (item["distance_px"] for item in coordinate_differences),
        default=0.0,
    )
    result = {
        "available": True,
        "tolerance_px": round_float(tolerance_px),
        "max_coordinate_delta_px": round_float(max_coordinate_delta),
        "coordinate_difference_count": len(coordinate_differences),
        "coordinate_differences": coordinate_differences,
        "instance_iou": instance_iou,
        "instance_iou_min": min(
            (item["bbox_iou"] for item in instance_iou), default=1.0
        ),
        "pck_delta_spectrum": pck_delta_spectrum,
        "overall_pck": {
            "exact": exact["metrics"]["pck"],
            "approx": approx["metrics"]["pck"],
            "delta_exact_minus_approx": round_float(
                float(exact["metrics"]["pck"]) - float(approx["metrics"]["pck"])
            ),
        },
        "drift_detected": bool(violations),
        "violations": violations,
    }
    if violations:
        raise PipelineError(
            "E_PATH_DRIFT",
            "cross_path.coordinate_differences",
            "exact and approximate peak coordinates exceed the declared tolerance",
            image_id=sample.get("image_id"),
            details={
                "tolerance_px": round_float(tolerance_px),
                "violations": violations,
            },
        )
    return result


def unavailable_reference(reasons: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "available": False,
        "reasons": reasons,
        "coordinate_differences": [],
        "instance_iou": [],
        "pck_delta_spectrum": [],
        "drift_detected": False,
        "violations": [],
    }
