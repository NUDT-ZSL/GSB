from __future__ import annotations

import math
from typing import Any

from .utils import round_float


def truth_instances(sample: dict[str, Any]) -> list[dict[str, Any]]:
    instances = []
    for skeleton in sorted(sample["skeletons"], key=lambda item: item["skeleton_id"]):
        instances.append(
            {
                "instance_id": f"gt:skel{skeleton['skeleton_id']:03d}",
                "skeleton_id": skeleton["skeleton_id"],
                "source": skeleton["source"],
                "keypoints": {
                    point["index"]: dict(point) for point in skeleton["keypoints"]
                },
            }
        )
    return instances


def grouped_instances(grouped: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for instance in grouped["instances"]:
        result.append(
            {
                "instance_id": instance["instance_id"],
                "skeleton_id": instance["skeleton_id"],
                "source": instance["source"],
                "keypoints": {
                    point["keypoint_index"]: dict(point)
                    for point in instance["keypoints"]
                },
            }
        )
    return result


def candidate_instances(sample: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for candidate in sorted(
        sample["candidates"], key=lambda item: (item["source"], item["candidate_id"])
    ):
        result.append(
            {
                "instance_id": candidate["candidate_id"],
                "skeleton_id": None,
                "source": candidate["source"],
                "keypoints": {
                    point["index"]: dict(point) for point in candidate["keypoints"]
                },
            }
        )
    return result


def _visible_indices(
    truth: dict[str, Any], matching_config: dict[str, Any]
) -> list[int]:
    exclude_invisible = bool(matching_config["exclude_invisible"])
    return sorted(
        index
        for index, point in truth["keypoints"].items()
        if point["visible"] or not exclude_invisible
    )


def pair_similarity(
    predicted: dict[str, Any],
    truth: dict[str, Any],
    sample: dict[str, Any],
    matching_config: dict[str, Any],
) -> dict[str, Any]:
    metric = matching_config["metric"]
    pck_threshold = float(matching_config["pck_threshold_px"])
    sigma_scale = float(matching_config["oks_sigma_scale"])
    height, width = sample["canvas_shape"]
    area = float(height * width)
    metadata = {item["index"]: item for item in sample["keypoint_meta"]}
    visible = _visible_indices(truth, matching_config)
    if not visible:
        return {
            "score": 0.0,
            "mean_distance_px": None,
            "evaluated_keypoints": 0,
            "first_keypoint_index": None,
            "hits": 0,
        }

    distances = []
    similarities = []
    hits = 0
    for keypoint_index in visible:
        truth_point = truth["keypoints"][keypoint_index]
        predicted_point = predicted["keypoints"].get(keypoint_index)
        if predicted_point is None:
            dist = math.inf
        else:
            dist = math.hypot(
                float(predicted_point["x"]) - float(truth_point["x"]),
                float(predicted_point["y"]) - float(truth_point["y"]),
            )
        distances.append(dist)
        if dist <= pck_threshold:
            hits += 1
        sigma = float(metadata.get(keypoint_index, {}).get("oks_sigma", sigma_scale))
        if math.isfinite(dist):
            similarities.append(math.exp(-(dist**2) / (2.0 * sigma**2 * area)))
        else:
            similarities.append(0.0)

    if metric == "pck":
        score = hits / len(visible)
    else:
        score = sum(similarities) / len(visible)
    finite_distances = [value for value in distances if math.isfinite(value)]
    return {
        "score": round_float(score),
        "mean_distance_px": (
            round_float(sum(finite_distances) / len(finite_distances))
            if finite_distances
            else None
        ),
        "evaluated_keypoints": len(visible),
        "first_keypoint_index": visible[0],
        "hits": hits,
    }


def _hungarian_min(cost: list[list[float]]) -> list[int]:
    row_count = len(cost)
    if row_count == 0:
        return []
    column_count = len(cost[0])
    if row_count > column_count:
        transposed = [
            [cost[row][column] for row in range(row_count)]
            for column in range(column_count)
        ]
        assignment = _hungarian_min(transposed)
        result = [-1] * row_count
        for transposed_row, transposed_column in enumerate(assignment):
            if transposed_column >= 0:
                result[transposed_column] = transposed_row
        return result

    u = [0.0] * (row_count + 1)
    v = [0.0] * (column_count + 1)
    p = [0] * (column_count + 1)
    way = [0] * (column_count + 1)
    for row in range(1, row_count + 1):
        p[0] = row
        column0 = 0
        min_values = [math.inf] * (column_count + 1)
        used = [False] * (column_count + 1)
        while True:
            used[column0] = True
            row0 = p[column0]
            delta = math.inf
            column1 = 0
            for column in range(1, column_count + 1):
                if used[column]:
                    continue
                current = cost[row0 - 1][column - 1] - u[row0] - v[column]
                if current < min_values[column] - 1e-15:
                    min_values[column] = current
                    way[column] = column0
                if min_values[column] < delta - 1e-15:
                    delta = min_values[column]
                    column1 = column
            for column in range(column_count + 1):
                if used[column]:
                    u[p[column]] += delta
                    v[column] -= delta
                else:
                    min_values[column] -= delta
            column0 = column1
            if p[column0] == 0:
                break
        while True:
            column1 = way[column0]
            p[column0] = p[column1]
            column0 = column1
            if column0 == 0:
                break
    assignment = [-1] * row_count
    for column in range(1, column_count + 1):
        if p[column] != 0:
            assignment[p[column] - 1] = column - 1
    return assignment


def match_instances(
    predictions: list[dict[str, Any]],
    truths: list[dict[str, Any]],
    sample: dict[str, Any],
    matching_config: dict[str, Any],
    strategy: str,
) -> dict[str, Any]:
    predictions = sorted(
        predictions,
        key=lambda item: (item["source"], item["instance_id"]),
    )
    truths = sorted(
        truths,
        key=lambda item: (item["skeleton_id"], item["instance_id"]),
    )
    threshold = float(matching_config["threshold"])
    pairs: list[dict[str, Any]] = []
    matrix_cells = len(predictions) * len(truths)
    if matrix_cells:
        for truth_index, truth in enumerate(truths):
            for prediction_index, prediction in enumerate(predictions):
                detail = pair_similarity(prediction, truth, sample, matching_config)
                pairs.append(
                    {
                        "truth_index": truth_index,
                        "prediction_index": prediction_index,
                        "truth_instance_id": truth["instance_id"],
                        "prediction_instance_id": prediction["instance_id"],
                        "prediction_source": prediction["source"],
                        **detail,
                    }
                )

    selected: list[dict[str, Any]] = []
    if strategy == "hungarian":
        if predictions and truths:
            scores = [
                [
                    pair["score"]
                    for pair in pairs
                    if pair["truth_index"] == truth_index
                ]
                for truth_index in range(len(truths))
            ]
            max_score = max(max(row) for row in scores)
            count = max(len(predictions), len(truths))
            epsilon = 1e-10
            tie_bonus = {
                (pair["truth_index"], pair["prediction_index"]): (
                    epsilon * (len(predictions) - pair["prediction_index"])
                    + epsilon * epsilon * (len(truths) - pair["truth_index"])
                )
                for pair in pairs
            }
            if len(predictions) <= len(truths):
                cost = [
                    [
                        max_score
                        - scores[truth_index][prediction_index]
                        - tie_bonus[(truth_index, prediction_index)]
                        for prediction_index in range(len(predictions))
                    ]
                    for truth_index in range(len(truths))
                ]
                assigned_columns = _hungarian_min(cost)
                for truth_index, prediction_index in enumerate(assigned_columns):
                    if prediction_index >= 0:
                        pair = pairs[truth_index * len(predictions) + prediction_index]
                        selected.append(pair)
            else:
                cost = [
                    [
                        max_score
                        - scores[truth_index][prediction_index]
                        - tie_bonus[(truth_index, prediction_index)]
                        for truth_index in range(len(truths))
                    ]
                    for prediction_index in range(len(predictions))
                ]
                assigned_columns = _hungarian_min(cost)
                for prediction_index, truth_index in enumerate(assigned_columns):
                    if truth_index >= 0:
                        pair = pairs[truth_index * len(predictions) + prediction_index]
                        selected.append(pair)
    elif strategy == "chunked_greedy":
        chunk_size = max(1, int(matching_config.get("approx_chunk_size", 2)))
        global_used_prediction: set[int] = set()
        for chunk_start in range(0, len(truths), chunk_size):
            chunk_end = min(len(truths), chunk_start + chunk_size)
            local_pairs = [
                pair
                for pair in pairs
                if chunk_start <= pair["truth_index"] < chunk_end
                and pair["prediction_index"] not in global_used_prediction
            ]
            local_pairs.sort(
                key=lambda pair: (
                    -pair["score"],
                    pair["prediction_source"],
                    pair["first_keypoint_index"]
                    if pair["first_keypoint_index"] is not None
                    else -1,
                    pair["prediction_instance_id"],
                )
            )
            used_truth: set[int] = set()
            for pair in local_pairs:
                if (
                    pair["truth_index"] in used_truth
                    or pair["prediction_index"] in global_used_prediction
                ):
                    continue
                used_truth.add(pair["truth_index"])
                global_used_prediction.add(pair["prediction_index"])
                selected.append(pair)
    else:
        raise ValueError(f"unsupported matching strategy: {strategy}")

    selected.sort(key=lambda pair: pair["truth_index"])
    assignments = []
    assigned_truths = set()
    assigned_predictions = set()
    for pair in selected:
        accepted = pair["score"] >= threshold
        assignments.append(
            {
                "truth_instance_id": pair["truth_instance_id"],
                "predicted_instance_id": pair["prediction_instance_id"],
                "prediction_source": pair["prediction_source"],
                "score": pair["score"],
                "accepted": accepted,
                "mean_distance_px": pair["mean_distance_px"],
                "evaluated_keypoints": pair["evaluated_keypoints"],
                "match_tie_break": [
                    pair["prediction_source"],
                    pair["prediction_instance_id"],
                    pair["first_keypoint_index"],
                ],
            }
        )
        if accepted:
            assigned_truths.add(pair["truth_index"])
            assigned_predictions.add(pair["prediction_index"])

    return {
        "metric": matching_config["metric"],
        "strategy": strategy,
        "tie_break_order": [
            "match_score_desc",
            "candidate_source_asc",
            "keypoint_index_asc",
            "instance_id_asc",
        ],
        "threshold": round_float(threshold),
        "prediction_count": len(predictions),
        "truth_count": len(truths),
        "matrix_cells": matrix_cells,
        "assignments": assignments,
        "unmatched_truth_instance_ids": [
            truth["instance_id"]
            for index, truth in enumerate(truths)
            if index not in assigned_truths
        ],
        "unmatched_prediction_instance_ids": [
            prediction["instance_id"]
            for index, prediction in enumerate(predictions)
            if index not in assigned_predictions
        ],
        "coverage_complete": len(assignments) == min(len(predictions), len(truths)),
    }
