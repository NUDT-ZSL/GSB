from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .errors import TrackingError


def _hungarian_max_weight(weights: list[list[int]]) -> tuple[int, list[tuple[int, int]]]:
    """Maximum-weight assignment for a non-negative rectangular matrix."""

    if not weights or not weights[0]:
        return 0, []
    row_count = len(weights)
    column_count = len(weights[0])
    size = max(row_count, column_count)
    maximum = max(max(row) for row in weights) if weights else 0
    cost = [
        [
            maximum - (weights[row][column] if row < row_count and column < column_count else 0)
            for column in range(size)
        ]
        for row in range(size)
    ]

    u = [0] * (size + 1)
    v = [0] * (size + 1)
    p = [0] * (size + 1)
    way = [0] * (size + 1)
    for row in range(1, size + 1):
        p[0] = row
        column0 = 0
        minv = [10**18] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[column0] = True
            row0 = p[column0]
            delta = 10**18
            column1 = 0
            for column in range(1, size + 1):
                if used[column]:
                    continue
                current = cost[row0 - 1][column - 1] - u[row0] - v[column]
                if current < minv[column]:
                    minv[column] = current
                    way[column] = column0
                if minv[column] < delta:
                    delta = minv[column]
                    column1 = column
            for column in range(size + 1):
                if used[column]:
                    u[p[column]] += delta
                    v[column] -= delta
                else:
                    minv[column] -= delta
            column0 = column1
            if p[column0] == 0:
                break
        while True:
            column1 = way[column0]
            p[column0] = p[column1]
            column0 = column1
            if column0 == 0:
                break

    assignment: list[tuple[int, int]] = []
    total = 0
    for column in range(1, size + 1):
        row = p[column]
        if row <= row_count and column <= column_count:
            weight = weights[row - 1][column - 1]
            if weight > 0:
                assignment.append((row - 1, column - 1))
                total += weight
    return total, sorted(assignment)


def _primary_mapping(
    assignments: list[dict[str, Any]],
) -> tuple[dict[int, int], dict[tuple[int, int], int]]:
    edges: Counter[tuple[int, int]] = Counter()
    for row in assignments:
        edges[(int(row["pred_track_id"]), int(row["track_id_true"]))] += 1
    truths_by_pred: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for (pred_id, truth_id), count in edges.items():
        truths_by_pred[pred_id].append((truth_id, count))
    mapping: dict[int, int] = {}
    for pred_id, candidates in truths_by_pred.items():
        mapping[pred_id] = max(candidates, key=lambda item: (item[1], -item[0]))[0]
    return mapping, dict(edges)


def compute_metrics(
    *,
    seq_id: str,
    revision: int,
    assignments: list[dict[str, Any]],
    truth_by_frame: dict[int, tuple[Any, ...]],
    completion_rows: list[dict[str, Any]],
    gap_reports: list[dict[str, Any]],
    segment_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    real_rows = sorted(
        assignments, key=lambda row: (row["frame_index"], row["detection_index"])
    )
    mapping, edges = _primary_mapping(real_rows)
    ground_truth_count = sum(len(items) for items in truth_by_frame.values())
    assigned_detections = len(real_rows)
    true_positives = sum(
        1
        for row in real_rows
        if mapping.get(int(row["pred_track_id"])) == int(row["track_id_true"])
    )
    false_positives = len(real_rows) - true_positives
    false_negatives = ground_truth_count - true_positives
    association_mismatches = assigned_detections - true_positives

    truth_to_predictions: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in real_rows:
        truth_to_predictions[int(row["track_id_true"])].append(row)
    switch_events: list[dict[str, Any]] = []
    for truth_id in sorted(truth_to_predictions):
        observations = sorted(
            truth_to_predictions[truth_id], key=lambda row: row["frame_index"]
        )
        previous_prediction: int | None = None
        for row in observations:
            prediction = int(row["pred_track_id"])
            if previous_prediction is not None and prediction != previous_prediction:
                switch_events.append(
                    {
                        "truth_track_id": truth_id,
                        "frame_index": int(row["frame_index"]),
                        "from_pred_track_id": previous_prediction,
                        "to_pred_track_id": prediction,
                    }
                )
            previous_prediction = prediction
    switch_count = len(switch_events)

    pred_tracks = sorted({key[0] for key in edges})
    truth_tracks = sorted({key[1] for key in edges})
    weight_matrix = [
        [int(edges.get((pred_id, truth_id), 0)) for truth_id in truth_tracks]
        for pred_id in pred_tracks
    ]
    idtp, _identity_assignment = _hungarian_max_weight(weight_matrix)
    idfp = assigned_detections - idtp
    idfn = ground_truth_count - idtp
    idf_denominator = 2 * idtp + idfp + idfn
    idf1 = (2.0 * idtp / idf_denominator) if idf_denominator else 0.0
    mota_denominator = ground_truth_count
    mota = (
        1.0 - (false_negatives + false_positives + switch_count) / mota_denominator
        if mota_denominator
        else 0.0
    )

    per_frame: list[dict[str, Any]] = []
    switches_by_frame = Counter(item["frame_index"] for item in switch_events)
    all_frames = sorted(set(truth_by_frame) | {int(row["frame_index"]) for row in real_rows})
    for frame_index in all_frames:
        frame_rows = [row for row in real_rows if row["frame_index"] == frame_index]
        frame_tp = sum(
            1
            for row in frame_rows
            if mapping.get(int(row["pred_track_id"])) == int(row["track_id_true"])
        )
        frame_gt = len(truth_by_frame.get(frame_index, ()))
        frame_fp = len(frame_rows) - frame_tp
        frame_fn = frame_gt - frame_tp
        per_frame.append(
            {
                "frame_index": frame_index,
                "ground_truth": frame_gt,
                "detections": len(frame_rows),
                "true_positives": frame_tp,
                "false_positives": frame_fp,
                "false_negatives": frame_fn,
                "id_switches": switches_by_frame.get(frame_index, 0),
            }
        )

    identity_mapping = [
        {
            "pred_track_id": pred_id,
            "primary_truth_track_id": truth_id,
            "matched_detections": sum(
                count
                for (edge_pred, edge_truth), count in edges.items()
                if edge_pred == pred_id and edge_truth == truth_id
            ),
        }
        for pred_id, truth_id in sorted(mapping.items())
    ]
    completed_frames = len(completion_rows)
    completed_tracks = sorted({int(row["pred_track_id"]) for row in completion_rows})

    return {
        "schema": "mt-metrics-v1",
        "seq_id": seq_id,
        "revision": revision,
        "frame_count": len(truth_by_frame),
        "ground_truth_count": ground_truth_count,
        "detection_count": len(real_rows),
        "assigned_detection_count": assigned_detections,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "association_mismatches": association_mismatches,
        "id_switches": switch_count,
        "switch_events": switch_events,
        "mota": round(mota, 10),
        "idtp": idtp,
        "idfp": idfp,
        "idfn": idfn,
        "idf1": round(idf1, 10),
        "identity_mapping": identity_mapping,
        "per_frame": per_frame,
        "completion": {
            "completed_frames": completed_frames,
            "completed_tracks": completed_tracks,
            "gap_count": len(gap_reports),
            "completed_hits": completed_frames,
            "real_hits": assigned_detections,
            "completed_are_scored_separately": True,
        },
        "segments": {
            "track_segment_count": len(segment_rows),
            "raw_fragment_count": sum(
                int(row["fragment_count_before_completion"])
                for row in segment_rows
            ),
            "merged_fragment_count": sum(
                int(row["fragment_count_after_completion"]) for row in segment_rows
            ),
        },
    }


def verify_metric_recompute(
    metrics: dict[str, Any],
    *,
    assignments: list[dict[str, Any]],
    truth_by_frame: dict[int, tuple[Any, ...]],
    completion_rows: list[dict[str, Any]],
    gap_reports: list[dict[str, Any]],
    segment_rows: list[dict[str, Any]],
) -> None:
    recomputed = compute_metrics(
        seq_id=metrics["seq_id"],
        revision=metrics["revision"],
        assignments=assignments,
        truth_by_frame=truth_by_frame,
        completion_rows=completion_rows,
        gap_reports=gap_reports,
        segment_rows=segment_rows,
    )
    keys = (
        "ground_truth_count",
        "detection_count",
        "true_positives",
        "false_positives",
        "false_negatives",
        "id_switches",
        "mota",
        "idtp",
        "idfp",
        "idfn",
        "idf1",
        "per_frame",
    )
    mismatched = [key for key in keys if recomputed.get(key) != metrics.get(key)]
    if mismatched:
        raise TrackingError(
            "E_INCONSISTENT",
            "metrics cannot be reproduced from frame-level detail",
            seq_id=metrics["seq_id"],
            category="metrics_recompute_mismatch",
            context={"fields": mismatched},
        )
