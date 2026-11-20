from __future__ import annotations

import math
from typing import Any

from .metrics import recompute_from_keypoint_details
from .utils import round_float


def _issue(
    image_id: str | None,
    category: str,
    field: str,
    message: str,
    details: Any | None = None,
) -> dict[str, Any]:
    result = {
        "code": "E_INCONSISTENT",
        "severity": "error",
        "category": category,
        "field": field,
        "message": message,
    }
    if image_id is not None:
        result["image_id"] = image_id
    if details is not None:
        result["details"] = details
    return result


def self_check_report(
    report: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    checks = []
    expected_images = int(config["num_images"])
    images = report.get("images", [])
    if len(images) != expected_images:
        issues.append(
            _issue(
                None,
                "IMAGE_COVERAGE",
                "images",
                "report image count does not match the configured image count",
                {"expected": expected_images, "actual": len(images)},
            )
        )
    checks.append(
        {
            "name": "image_coverage",
            "ok": len(images) == expected_images,
            "expected": expected_images,
            "actual": len(images),
        }
    )

    for image in images:
        image_id = image["image_id"]
        selected = image["selected_path_result"]
        decoded = selected["decoded"]
        grouped = selected["grouped"]
        metrics = selected["metrics"]
        decision = image["decision"]
        selected_by_channel = {}
        for peak in decoded["peaks"]:
            selected_by_channel.setdefault(peak["channel_index"], peak)
        expected_channels = set(range(image["num_channels"]))
        channel_ok = set(selected_by_channel) == expected_channels
        if not channel_ok:
            issues.append(
                _issue(
                    image_id,
                    "PEAK_CHANNEL_COVERAGE",
                    "selected_path_result.decoded.peaks",
                    "decoded peak set does not cover every heatmap channel",
                    {
                        "missing": sorted(expected_channels - set(selected_by_channel)),
                        "extra": sorted(set(selected_by_channel) - expected_channels),
                    },
                )
            )
        checks.append(
            {
                "name": "peak_channel_coverage",
                "image_id": image_id,
                "ok": channel_ok,
            }
        )

        grouped_peak_ids = [
            point["peak_id"]
            for instance in grouped["instances"]
            for point in instance["keypoints"]
        ]
        selected_peak_ids = [
            peak["peak_id"] for peak in selected_by_channel.values()
        ]
        grouping_ok = sorted(grouped_peak_ids) == sorted(selected_peak_ids) and len(
            grouped_peak_ids
        ) == len(set(grouped_peak_ids))
        if not grouping_ok:
            issues.append(
                _issue(
                    image_id,
                    "GROUPING_PEAK_COVERAGE",
                    "selected_path_result.grouped.instances",
                    "grouped keypoint peak set differs from decoded selected peaks",
                    {
                        "decoded_count": len(selected_peak_ids),
                        "grouped_count": len(grouped_peak_ids),
                        "grouped_unique_count": len(set(grouped_peak_ids)),
                    },
                )
            )
        checks.append(
            {
                "name": "grouping_peak_coverage",
                "image_id": image_id,
                "ok": grouping_ok,
            }
        )

        evidence_ok = True
        bin_size = float(config["heatmap"]["group_bin_size"])
        for instance in grouped["instances"]:
            skeleton_id = instance["skeleton_id"]
            expected_instance_channels = {
                skeleton_id * image["num_keypoints"] + keypoint_index
                for keypoint_index in range(image["num_keypoints"])
            }
            if {
                point["channel_index"] for point in instance["keypoints"]
            } != expected_instance_channels:
                evidence_ok = False
                break
            point_by_index = {
                point["keypoint_index"]: point for point in instance["keypoints"]
            }
            for evidence in instance["edge_evidence"]:
                left, right = evidence["keypoint_pair"]
                left_point = point_by_index[left]
                right_point = point_by_index[right]
                if evidence["peak_ids"] != [
                    left_point["peak_id"],
                    right_point["peak_id"],
                ]:
                    evidence_ok = False
                    break
                intensity_delta = round_float(
                    abs(
                        float(left_point["intensity"])
                        - float(right_point["intensity"])
                    )
                )
                pair_distance = math.hypot(
                    float(left_point["x"]) - float(right_point["x"]),
                    float(left_point["y"]) - float(right_point["y"]),
                )
                if abs(intensity_delta - evidence["intensity_delta"]) > 1e-9:
                    evidence_ok = False
                    break
                if abs(round_float(pair_distance) - evidence["distance"]) > 1e-9:
                    evidence_ok = False
                    break
                if selected["path"] == "approx":
                    left_x = math.floor(float(left_point["x"]) / bin_size) * bin_size
                    left_y = math.floor(float(left_point["y"]) / bin_size) * bin_size
                    right_x = math.floor(float(right_point["x"]) / bin_size) * bin_size
                    right_y = math.floor(float(right_point["y"]) / bin_size) * bin_size
                    binned_distance = math.hypot(left_x - right_x, left_y - right_y)
                    if (
                        abs(round_float(binned_distance) - evidence["binned_distance"])
                        > 1e-9
                    ):
                        evidence_ok = False
                        break
            if not evidence_ok:
                break
        if not evidence_ok:
            issues.append(
                _issue(
                    image_id,
                    "GROUPING_EVIDENCE_MISMATCH",
                    "selected_path_result.grouped.instances.edge_evidence",
                    "skeleton edge evidence cannot be recomputed from grouped peaks",
                )
            )
        checks.append(
            {
                "name": "grouping_evidence_recompute",
                "image_id": image_id,
                "ok": evidence_ok,
            }
        )

        expected_keypoint_count = (
            image["summary"]["skeleton_count"] * image["num_keypoints"]
        )
        keypoint_count_ok = len(grouped_peak_ids) == expected_keypoint_count
        if not keypoint_count_ok:
            issues.append(
                _issue(
                    image_id,
                    "KEYPOINT_COVERAGE",
                    "selected_path_result.grouped.instances",
                    "grouped keypoint count does not match all skeletons",
                    {
                        "expected": expected_keypoint_count,
                        "actual": len(grouped_peak_ids),
                    },
                )
            )
        checks.append(
            {
                "name": "keypoint_coverage",
                "image_id": image_id,
                "ok": keypoint_count_ok,
            }
        )

        details = metrics["keypoint_details"]
        matched_peak_ids = [
            detail["predicted_peak_id"]
            for detail in details
            if detail["matched_instance"]
        ]
        matching_ok = all(peak_id in set(selected_peak_ids) for peak_id in matched_peak_ids)
        for assignment in selected["matching"]["assignments"]:
            if assignment["accepted"] and assignment["predicted_instance_id"] not in {
                instance["instance_id"] for instance in grouped["instances"]
            }:
                matching_ok = False
        if not matching_ok:
            issues.append(
                _issue(
                    image_id,
                    "MATCHING_PEAK_COVERAGE",
                    "selected_path_result.metrics.keypoint_details",
                    "matching refers to keypoints or instances outside the grouped peak set",
                )
            )
        checks.append(
            {
                "name": "matching_peak_coverage",
                "image_id": image_id,
                "ok": matching_ok,
            }
        )

        recomputed = recompute_from_keypoint_details(details)
        metrics_ok = (
            metrics["total_keypoints"] == recomputed["total_keypoints"]
            and metrics["visible_keypoints"] == recomputed["visible_keypoints"]
            and metrics["evaluated_keypoints"] == recomputed["evaluated_keypoints"]
            and metrics["excluded_invisible_keypoints"]
            == recomputed["excluded_invisible_keypoints"]
            and metrics["pck_hits"] == recomputed["pck_hits"]
            and abs(float(metrics["pck"]) - float(recomputed["pck"])) <= 1e-9
        )
        if not metrics_ok:
            issues.append(
                _issue(
                    image_id,
                    "METRIC_MISMATCH",
                    "selected_path_result.metrics",
                    "metrics cannot be recomputed from per-keypoint details",
                    {"reported": metrics, "recomputed": recomputed},
                )
            )
        checks.append(
            {
                "name": "metric_recompute",
                "image_id": image_id,
                "ok": metrics_ok,
            }
        )

        exclude_invisible = bool(config["matching"]["exclude_invisible"])
        visibility_ok = all(
            (
                detail["excluded"] == (not detail["visible"])
                if exclude_invisible
                else not detail["excluded"]
            )
            for detail in details
        )
        visibility_ok = visibility_ok and (
            recomputed["evaluated_keypoints"]
            + recomputed["excluded_invisible_keypoints"]
            == recomputed["total_keypoints"]
        )
        if not visibility_ok:
            issues.append(
                _issue(
                    image_id,
                    "VISIBILITY_DENOMINATOR",
                    "selected_path_result.metrics.keypoint_details",
                    "excluded keypoints and metric denominator are inconsistent",
                )
            )
        checks.append(
            {
                "name": "visibility_denominator",
                "image_id": image_id,
                "ok": visibility_ok,
            }
        )

        reference = decision["exact_reference"]
        cross_path = image["cross_path"]
        reference_ok = bool(reference["available"]) == bool(cross_path["available"])
        if reference["available"] and (
            len(cross_path.get("coordinate_differences", [])) != image["num_channels"]
        ):
            reference_ok = False
        if not reference["available"] and not cross_path.get("reasons"):
            reference_ok = False
        if not reference_ok:
            issues.append(
                _issue(
                    image_id,
                    "REFERENCE_DECLARATION",
                    "cross_path.available",
                    "exact reference availability is not reflected by the cross-path record",
                )
            )
        checks.append(
            {
                "name": "reference_declaration",
                "image_id": image_id,
                "ok": reference_ok,
            }
        )

        degradation_ok = (not decision["degraded"]) or bool(
            decision["degradation_categories"]
        )
        checks.append(
            {
                "name": "degradation_marker",
                "image_id": image_id,
                "ok": degradation_ok,
            }
        )
        if not degradation_ok:
            issues.append(
                _issue(
                    image_id,
                    "DEGRADATION_MARKER",
                    "decision.degradation_categories",
                    "degraded image is missing degradation categories",
                )
            )

    summary = report["summary"]
    coverage_ok = summary["covered_image_count"] == summary["image_count"]
    if not coverage_ok:
        issues.append(
            _issue(
                None,
                "SELECTED_PATH_COVERAGE",
                "summary.covered_image_count",
                "selected path does not cover every image",
            )
        )
    checks.append(
        {
            "name": "selected_path_coverage",
            "ok": coverage_ok,
        }
    )

    total_details = [
        detail
        for image in images
        for detail in image["selected_path_result"]["metrics"]["keypoint_details"]
    ]
    aggregate_recompute = recompute_from_keypoint_details(total_details)
    aggregate = summary["metrics"]
    aggregate_ok = (
        aggregate["total_keypoints"] == aggregate_recompute["total_keypoints"]
        and aggregate["visible_keypoints"] == aggregate_recompute["visible_keypoints"]
        and aggregate["evaluated_keypoints"]
        == aggregate_recompute["evaluated_keypoints"]
        and aggregate["excluded_invisible_keypoints"]
        == aggregate_recompute["excluded_invisible_keypoints"]
        and aggregate["pck_hits"] == aggregate_recompute["pck_hits"]
        and abs(float(aggregate["pck"]) - float(aggregate_recompute["pck"])) <= 1e-9
    )
    if not aggregate_ok:
        issues.append(
            _issue(
                None,
                "AGGREGATE_METRIC_MISMATCH",
                "summary.metrics",
                "aggregate metrics cannot be recomputed from image keypoint details",
                {"reported": aggregate, "recomputed": aggregate_recompute},
            )
        )
    checks.append(
        {
            "name": "aggregate_metric_recompute",
            "ok": aggregate_ok,
        }
    )

    return {
        "ok": not issues,
        "check_count": len(checks),
        "failed_check_count": sum(1 for check in checks if not check["ok"]),
        "checks": checks,
        "issues": issues,
    }
