from __future__ import annotations

import math
from typing import Any

from .matching import pair_similarity
from .utils import round_float


def compute_metrics(
    sample: dict[str, Any],
    truths: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    matching: dict[str, Any],
    matching_config: dict[str, Any],
) -> dict[str, Any]:
    prediction_by_id = {item["instance_id"]: item for item in predictions}
    accepted_by_truth = {
        item["truth_instance_id"]: item
        for item in matching["assignments"]
        if item["accepted"]
    }
    pck_threshold = float(matching_config["pck_threshold_px"])
    exclude_invisible = bool(matching_config["exclude_invisible"])
    metadata = {item["index"]: item for item in sample["keypoint_meta"]}
    height, width = sample["canvas_shape"]
    area = float(height * width)
    sigma_scale = float(matching_config["oks_sigma_scale"])

    keypoint_details = []
    per_instance = []
    matched_instance_count = 0
    unmatched_instance_count = 0
    matched_oks_sum = 0.0
    oks_recall_count = 0
    total_keypoints = 0
    visible_keypoints = 0
    excluded_keypoints = 0
    evaluated_keypoints = 0
    pck_hits = 0
    distance_sum = 0.0
    distance_count = 0

    for truth in truths:
        assignment = accepted_by_truth.get(truth["instance_id"])
        predicted = (
            prediction_by_id.get(assignment["predicted_instance_id"])
            if assignment is not None
            else None
        )
        if assignment is not None:
            matched_instance_count += 1
        else:
            unmatched_instance_count += 1

        instance_pair_similarity = None
        if predicted is not None:
            instance_pair_similarity = pair_similarity(
                predicted, truth, sample, matching_config
            )
            matched_oks_sum += float(instance_pair_similarity["score"])
            if float(instance_pair_similarity["score"]) >= float(
                matching_config["threshold"]
            ):
                oks_recall_count += 1

        instance_evaluated = 0
        instance_hits = 0
        for keypoint_index in sorted(truth["keypoints"]):
            truth_point = truth["keypoints"][keypoint_index]
            is_visible = bool(truth_point["visible"])
            included = is_visible or not exclude_invisible
            total_keypoints += 1
            if is_visible:
                visible_keypoints += 1
            if not included:
                excluded_keypoints += 1
            predicted_point = (
                predicted["keypoints"].get(keypoint_index)
                if predicted is not None
                else None
            )
            distance_px = None
            oks_similarity = 0.0
            if included and predicted_point is not None:
                distance_px = math.hypot(
                    float(predicted_point["x"]) - float(truth_point["x"]),
                    float(predicted_point["y"]) - float(truth_point["y"]),
                )
                sigma = float(metadata.get(keypoint_index, {}).get("oks_sigma", sigma_scale))
                oks_similarity = math.exp(
                    -(distance_px**2) / (2.0 * sigma**2 * area)
                )
            hit = bool(
                included
                and distance_px is not None
                and distance_px <= pck_threshold
            )
            if included:
                evaluated_keypoints += 1
                instance_evaluated += 1
                if hit:
                    pck_hits += 1
                    instance_hits += 1
                if distance_px is not None:
                    distance_sum += distance_px
                    distance_count += 1
            keypoint_details.append(
                {
                    "image_id": sample["image_id"],
                    "truth_instance_id": truth["instance_id"],
                    "predicted_instance_id": (
                        assignment["predicted_instance_id"]
                        if assignment is not None
                        else None
                    ),
                    "keypoint_index": keypoint_index,
                    "visible": is_visible,
                    "excluded": not included,
                    "evaluated": included,
                    "matched_instance": assignment is not None,
                    "predicted_peak_id": (
                        predicted_point.get("peak_id") if predicted_point else None
                    ),
                    "predicted_x": (
                        predicted_point["x"] if predicted_point is not None else None
                    ),
                    "predicted_y": (
                        predicted_point["y"] if predicted_point is not None else None
                    ),
                    "truth_x": truth_point["x"],
                    "truth_y": truth_point["y"],
                    "distance_px": (
                        round_float(distance_px) if distance_px is not None else None
                    ),
                    "pck_threshold_px": round_float(pck_threshold),
                    "hit": hit,
                    "oks_similarity": round_float(oks_similarity),
                }
            )

        per_instance.append(
            {
                "truth_instance_id": truth["instance_id"],
                "predicted_instance_id": (
                    assignment["predicted_instance_id"] if assignment is not None else None
                ),
                "matched": assignment is not None,
                "assignment_score": (
                    assignment["score"] if assignment is not None else None
                ),
                "pair_oks": (
                    instance_pair_similarity["score"]
                    if instance_pair_similarity is not None
                    else 0.0
                ),
                "evaluated_keypoints": instance_evaluated,
                "pck_hits": instance_hits,
                "pck": (
                    round_float(instance_hits / instance_evaluated)
                    if instance_evaluated
                    else 0.0
                ),
            }
        )

    per_keypoint: dict[str, dict[str, Any]] = {}
    for detail in keypoint_details:
        key = str(detail["keypoint_index"])
        entry = per_keypoint.setdefault(
            key,
            {
                "evaluated_keypoints": 0,
                "excluded_keypoints": 0,
                "pck_hits": 0,
                "oks_sum": 0.0,
                "distance_sum_px": 0.0,
                "distance_count": 0,
            },
        )
        if detail["evaluated"]:
            entry["evaluated_keypoints"] += 1
            if detail["hit"]:
                entry["pck_hits"] += 1
            if detail["distance_px"] is not None:
                entry["distance_sum_px"] += detail["distance_px"]
                entry["distance_count"] += 1
            entry["oks_sum"] += detail["oks_similarity"]
        else:
            entry["excluded_keypoints"] += 1
    per_keypoint_output = {}
    for key in sorted(per_keypoint, key=int):
        entry = per_keypoint[key]
        per_keypoint_output[key] = {
            "evaluated_keypoints": entry["evaluated_keypoints"],
            "excluded_keypoints": entry["excluded_keypoints"],
            "pck_hits": entry["pck_hits"],
            "pck": (
                round_float(entry["pck_hits"] / entry["evaluated_keypoints"])
                if entry["evaluated_keypoints"]
                else 0.0
            ),
            "oks_sum": round_float(entry["oks_sum"]),
            "mean_oks": (
                round_float(entry["oks_sum"] / entry["evaluated_keypoints"])
                if entry["evaluated_keypoints"]
                else 0.0
            ),
            "mean_distance_px": (
                round_float(entry["distance_sum_px"] / entry["distance_count"])
                if entry["distance_count"]
                else None
            ),
        }

    return {
        "metric_config": {
            "matching_metric": matching_config["metric"],
            "match_threshold": round_float(float(matching_config["threshold"])),
            "pck_threshold_px": round_float(pck_threshold),
            "exclude_invisible": exclude_invisible,
        },
        "total_keypoints": total_keypoints,
        "visible_keypoints": visible_keypoints,
        "excluded_invisible_keypoints": excluded_keypoints,
        "evaluated_keypoints": evaluated_keypoints,
        "pck_hits": pck_hits,
        "pck": (
            round_float(pck_hits / evaluated_keypoints)
            if evaluated_keypoints
            else 0.0
        ),
        "mean_distance_px_matched": (
            round_float(distance_sum / distance_count)
            if distance_count
            else None
        ),
        "matched_instance_count": matched_instance_count,
        "unmatched_truth_instance_count": unmatched_instance_count,
        "matched_oks_sum": round_float(matched_oks_sum),
        "mean_matched_oks": (
            round_float(matched_oks_sum / matched_instance_count)
            if matched_instance_count
            else 0.0
        ),
        "oks_recall_count": oks_recall_count,
        "oks_recall": (
            round_float(oks_recall_count / matched_instance_count)
            if matched_instance_count
            else 0.0
        ),
        "per_instance": per_instance,
        "per_keypoint": per_keypoint_output,
        "keypoint_details": keypoint_details,
    }


def aggregate_metrics(metric_reports: list[dict[str, Any]]) -> dict[str, Any]:
    total_keypoints = 0
    visible_keypoints = 0
    excluded_keypoints = 0
    evaluated_keypoints = 0
    pck_hits = 0
    matched_instance_count = 0
    unmatched_truth_instance_count = 0
    matched_oks_sum = 0.0
    oks_recall_count = 0
    per_keypoint: dict[str, dict[str, Any]] = {}
    for report in metric_reports:
        total_keypoints += report["total_keypoints"]
        visible_keypoints += report["visible_keypoints"]
        excluded_keypoints += report["excluded_invisible_keypoints"]
        evaluated_keypoints += report["evaluated_keypoints"]
        pck_hits += report["pck_hits"]
        matched_instance_count += report["matched_instance_count"]
        unmatched_truth_instance_count += report["unmatched_truth_instance_count"]
        matched_oks_sum += report["matched_oks_sum"]
        oks_recall_count += report["oks_recall_count"]
        for key, value in report["per_keypoint"].items():
            entry = per_keypoint.setdefault(
                key,
                {
                    "evaluated_keypoints": 0,
                    "excluded_keypoints": 0,
                    "pck_hits": 0,
                    "oks_sum": 0.0,
                },
            )
            entry["evaluated_keypoints"] += value["evaluated_keypoints"]
            entry["excluded_keypoints"] += value["excluded_keypoints"]
            entry["pck_hits"] += value["pck_hits"]
            entry["oks_sum"] += value["oks_sum"]
    per_keypoint_output = {}
    for key in sorted(per_keypoint, key=int):
        entry = per_keypoint[key]
        per_keypoint_output[key] = {
            "evaluated_keypoints": entry["evaluated_keypoints"],
            "excluded_keypoints": entry["excluded_keypoints"],
            "pck_hits": entry["pck_hits"],
            "pck": (
                round_float(entry["pck_hits"] / entry["evaluated_keypoints"])
                if entry["evaluated_keypoints"]
                else 0.0
            ),
            "oks_sum": round_float(entry["oks_sum"]),
            "mean_oks": (
                round_float(entry["oks_sum"] / entry["evaluated_keypoints"])
                if entry["evaluated_keypoints"]
                else 0.0
            ),
        }
    return {
        "total_keypoints": total_keypoints,
        "visible_keypoints": visible_keypoints,
        "excluded_invisible_keypoints": excluded_keypoints,
        "evaluated_keypoints": evaluated_keypoints,
        "pck_hits": pck_hits,
        "pck": (
            round_float(pck_hits / evaluated_keypoints)
            if evaluated_keypoints
            else 0.0
        ),
        "matched_instance_count": matched_instance_count,
        "unmatched_truth_instance_count": unmatched_truth_instance_count,
        "matched_oks_sum": round_float(matched_oks_sum),
        "mean_matched_oks": (
            round_float(matched_oks_sum / matched_instance_count)
            if matched_instance_count
            else 0.0
        ),
        "oks_recall": (
            round_float(oks_recall_count / matched_instance_count)
            if matched_instance_count
            else 0.0
        ),
        "per_keypoint": per_keypoint_output,
    }


def recompute_from_keypoint_details(
    keypoint_details: list[dict[str, Any]],
) -> dict[str, Any]:
    total = len(keypoint_details)
    visible = sum(1 for item in keypoint_details if item["visible"])
    evaluated = sum(1 for item in keypoint_details if item["evaluated"])
    excluded = total - evaluated
    hits = sum(1 for item in keypoint_details if item["evaluated"] and item["hit"])
    return {
        "total_keypoints": total,
        "visible_keypoints": visible,
        "evaluated_keypoints": evaluated,
        "excluded_invisible_keypoints": excluded,
        "pck_hits": hits,
        "pck": round_float(hits / evaluated) if evaluated else 0.0,
        "visibility_denominator_consistent": visible + excluded == total,
    }
