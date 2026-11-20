"""Frame/event metrics with fully recorded, replayable decision details."""

from __future__ import annotations

from typing import Any, Sequence

from .catalog import ACTIVE_EVENT_TYPES, EVENT_TYPE_IDS
from .types import TimelineSegment

ALL_LABELS = ("background", *ACTIVE_EVENT_TYPES)


def _safe_div(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def truth_events_from_frames(
    truth_frames: Sequence[dict[str, Any]],
    *,
    hop_samples: int,
    sample_rate: int,
    duration_samples: int,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for row in truth_frames:
        event_id = row["event_id"]
        if event_id is None or row["event_type"] == "background":
            if current is not None:
                events.append(current)
                current = None
            continue
        frame_index = int(row["frame_index"])
        if (
            current is None
            or current["truth_event_id"] != event_id
            or current["event_type"] != row["event_type"]
            or current["end_frame"] + 1 != frame_index
        ):
            if current is not None:
                events.append(current)
            current = {
                "truth_event_id": event_id,
                "event_type": row["event_type"],
                "start_frame": frame_index,
                "end_frame": frame_index,
            }
        else:
            current["end_frame"] = frame_index
    if current is not None:
        events.append(current)

    for index, event in enumerate(events):
        event["start_ms"] = round(
            event["start_frame"] * hop_samples * 1000.0 / sample_rate, 6
        )
        event["end_ms"] = round(
            min(duration_samples, (event["end_frame"] + 1) * hop_samples)
            * 1000.0
            / sample_rate,
            6,
        )
        event["event_index"] = index
    return events


def predicted_events_from_segments(
    segments: Sequence[TimelineSegment],
    *,
    dialog_id: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for segment in segments:
        if not segment.is_event:
            continue
        events.append(
            {
                "prediction_id": f"{dialog_id}_pred_{len(events):03d}",
                "event_type": segment.event_type,
                "start_frame": segment.start_frame,
                "end_frame": segment.end_frame,
                "start_ms": round(segment.start_ms, 6),
                "end_ms": round(segment.end_ms, 6),
                "confidence": round(segment.confidence, 9),
            }
        )
    return events


def _iou(first: dict[str, Any], second: dict[str, Any]) -> tuple[int, int, float]:
    intersection = max(
        0,
        min(first["end_frame"], second["end_frame"])
        - max(first["start_frame"], second["start_frame"])
        + 1,
    )
    union = (
        max(first["end_frame"], second["end_frame"])
        - min(first["start_frame"], second["start_frame"])
        + 1
    )
    return intersection, union, _safe_div(intersection, union)


def _match_events(
    truth_events: Sequence[dict[str, Any]],
    predicted_events: Sequence[dict[str, Any]],
    *,
    overlap_threshold: float,
    dialog_id: str,
) -> dict[str, Any]:
    truth_detail = [
        {
            "dialog_id": dialog_id,
            "truth_event_id": event["truth_event_id"],
            "event_type": event["event_type"],
            "start_frame": event["start_frame"],
            "end_frame": event["end_frame"],
            "start_ms": event["start_ms"],
            "end_ms": event["end_ms"],
            "matched_prediction_id": None,
            "overlap_frames": 0,
            "union_frames": 0,
            "iou": 0.0,
            "hit": False,
        }
        for event in truth_events
    ]
    prediction_detail = [
        {
            "dialog_id": dialog_id,
            "prediction_id": event["prediction_id"],
            "event_type": event["event_type"],
            "start_frame": event["start_frame"],
            "end_frame": event["end_frame"],
            "start_ms": event["start_ms"],
            "end_ms": event["end_ms"],
            "confidence": event.get("confidence", 0.0),
            "matched_truth_event_id": None,
            "iou": 0.0,
            "hit": False,
        }
        for event in predicted_events
    ]

    pairs: list[tuple[float, int, int, int, int]] = []
    for truth_index, truth_event in enumerate(truth_events):
        for prediction_index, prediction in enumerate(predicted_events):
            if truth_event["event_type"] != prediction["event_type"]:
                continue
            overlap, union, iou = _iou(truth_event, prediction)
            if iou >= overlap_threshold:
                pairs.append(
                    (
                        -iou,
                        truth_event["start_frame"],
                        prediction["start_frame"],
                        truth_index,
                        prediction_index,
                    )
                )
    pairs.sort()
    truth_used: set[int] = set()
    prediction_used: set[int] = set()
    for neg_iou, _, _, truth_index, prediction_index in pairs:
        if truth_index in truth_used or prediction_index in prediction_used:
            continue
        truth_used.add(truth_index)
        prediction_used.add(prediction_index)
        truth_event = truth_events[truth_index]
        prediction = predicted_events[prediction_index]
        overlap, union, iou = _iou(truth_event, prediction)
        truth_row = truth_detail[truth_index]
        prediction_row = prediction_detail[prediction_index]
        truth_row["matched_prediction_id"] = prediction["prediction_id"]
        truth_row["overlap_frames"] = overlap
        truth_row["union_frames"] = union
        truth_row["iou"] = round(iou, 9)
        truth_row["hit"] = True
        prediction_row["matched_truth_event_id"] = truth_event["truth_event_id"]
        prediction_row["iou"] = round(iou, 9)
        prediction_row["hit"] = True

    truth_count = len(truth_detail)
    prediction_count = len(prediction_detail)
    matched_count = sum(1 for event in truth_detail if event["hit"])
    event_recall = _safe_div(matched_count, truth_count)
    event_precision = _safe_div(matched_count, prediction_count)
    event_f1 = (
        2.0 * event_precision * event_recall / (event_precision + event_recall)
        if event_precision + event_recall
        else 0.0
    )

    by_type: dict[str, dict[str, int]] = {
        event_type: {
            "truth_count": 0,
            "prediction_count": 0,
            "matched_count": 0,
        }
        for event_type in ACTIVE_EVENT_TYPES
    }
    for event in truth_detail:
        by_type[event["event_type"]]["truth_count"] += 1
        if event["hit"]:
            by_type[event["event_type"]]["matched_count"] += 1
    for event in prediction_detail:
        by_type[event["event_type"]]["prediction_count"] += 1

    return {
        "overlap_threshold": overlap_threshold,
        "overlap_ratio_definition": "intersection_frames / union_frames (temporal IoU)",
        "matching_rule": (
            "same event_type, one-to-one greedy by (-iou, truth_start_frame, "
            "prediction_start_frame)"
        ),
        "truth_count": truth_count,
        "prediction_count": prediction_count,
        "matched_count": matched_count,
        "event_recall": event_recall,
        "event_precision": event_precision,
        "event_f1": event_f1,
        "by_type": by_type,
        "details": {
            "truth_events": truth_detail,
            "predicted_events": prediction_detail,
        },
    }


def compute_frame_metrics(frame_decisions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not frame_decisions:
        raise ValueError("frame_decisions must not be empty")
    total = len(frame_decisions)
    correct = 0
    confusion = {
        truth: {prediction: 0 for prediction in ALL_LABELS} for truth in ALL_LABELS
    }
    for decision in frame_decisions:
        truth = decision["truth_event_type"]
        prediction = decision["predicted_event_type"]
        if truth not in EVENT_TYPE_IDS or prediction not in EVENT_TYPE_IDS:
            raise ValueError("frame decision contains an uncontrolled event type")
        confusion[truth][prediction] += 1
        if truth == prediction:
            correct += 1

    truth_active = sum(1 for decision in frame_decisions if decision["truth_event_type"] != "background")
    predicted_active = sum(
        1 for decision in frame_decisions if decision["predicted_event_type"] != "background"
    )
    active_true_positive = sum(
        1
        for decision in frame_decisions
        if decision["truth_event_type"] != "background"
        and decision["predicted_event_type"] != "background"
    )

    per_type: dict[str, dict[str, Any]] = {}
    for event_type in ALL_LABELS:
        true_positive = confusion[event_type][event_type]
        truth_count = sum(confusion[event_type].values())
        predicted_count = sum(confusion[truth][event_type] for truth in ALL_LABELS)
        recall = _safe_div(true_positive, truth_count)
        precision = _safe_div(true_positive, predicted_count)
        f1 = (
            2.0 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        per_type[event_type] = {
            "truth_frames": truth_count,
            "predicted_frames": predicted_count,
            "true_positive_frames": true_positive,
            "recall": recall,
            "precision": precision,
            "f1": f1,
        }

    return {
        "total_frames": total,
        "correct_frames": correct,
        "frame_accuracy": _safe_div(correct, total),
        "truth_active_frames": truth_active,
        "predicted_active_frames": predicted_active,
        "active_true_positive_frames": active_true_positive,
        "event_frame_recall": _safe_div(active_true_positive, truth_active),
        "event_frame_precision": _safe_div(active_true_positive, predicted_active),
        "per_event_type": per_type,
        "confusion": confusion,
        "definitions": {
            "frame_accuracy": "exact_frame_label_matches / total_frames",
            "event_frame_recall": (
                "frames_with_truth_and_prediction_active / truth_active_frames"
            ),
            "event_frame_precision": (
                "frames_with_truth_and_prediction_active / predicted_active_frames"
            ),
            "empty_denominator": "ratio is 0.0; denominator is recorded in counts",
        },
    }


def compute_dialog_metrics(
    *,
    dialog_id: str,
    truth_frames: Sequence[dict[str, Any]],
    segments: Sequence[TimelineSegment],
    frame_decisions: Sequence[dict[str, Any]],
    hop_samples: int,
    sample_rate: int,
    duration_samples: int,
    overlap_threshold: float,
) -> dict[str, Any]:
    truth_events = truth_events_from_frames(
        truth_frames,
        hop_samples=hop_samples,
        sample_rate=sample_rate,
        duration_samples=duration_samples,
    )
    predicted_events = predicted_events_from_segments(segments, dialog_id=dialog_id)
    frame_metrics = compute_frame_metrics(frame_decisions)
    event_metrics = _match_events(
        truth_events,
        predicted_events,
        overlap_threshold=overlap_threshold,
        dialog_id=dialog_id,
    )
    return {
        "dialog_id": dialog_id,
        "frame": frame_metrics,
        "event": event_metrics,
        "truth_events": truth_events,
        "predicted_events": predicted_events,
        "frame_decisions": list(frame_decisions),
    }


def aggregate_metrics(dialog_metrics: Sequence[dict[str, Any]]) -> dict[str, Any]:
    all_frame_decisions: list[dict[str, Any]] = []
    for metrics in dialog_metrics:
        all_frame_decisions.extend(metrics["frame_decisions"])
    frame_metrics = compute_frame_metrics(all_frame_decisions)

    truth_detail: list[dict[str, Any]] = []
    prediction_detail: list[dict[str, Any]] = []
    by_type = {
        event_type: {
            "truth_count": 0,
            "prediction_count": 0,
            "matched_count": 0,
        }
        for event_type in ACTIVE_EVENT_TYPES
    }
    for metrics in dialog_metrics:
        event = metrics["event"]
        truth_detail.extend(event["details"]["truth_events"])
        prediction_detail.extend(event["details"]["predicted_events"])
        for event_type in ACTIVE_EVENT_TYPES:
            for key in ("truth_count", "prediction_count", "matched_count"):
                by_type[event_type][key] += event["by_type"][event_type][key]
    truth_count = len(truth_detail)
    prediction_count = len(prediction_detail)
    matched_count = sum(1 for event in truth_detail if event["hit"])
    recall = _safe_div(matched_count, truth_count)
    precision = _safe_div(matched_count, prediction_count)
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "frame": frame_metrics,
        "event": {
            "overlap_threshold": (
                dialog_metrics[0]["event"].get("overlap_threshold")
                if dialog_metrics
                else None
            ),
            "overlap_ratio_definition": (
                "intersection_frames / union_frames (temporal IoU)"
            ),
            "truth_count": truth_count,
            "prediction_count": prediction_count,
            "matched_count": matched_count,
            "event_recall": recall,
            "event_precision": precision,
            "event_f1": f1,
            "by_type": by_type,
            "details": {
                "truth_events": truth_detail,
                "predicted_events": prediction_detail,
            },
        },
    }


def metrics_from_recorded_details(
    frame_decisions: Sequence[dict[str, Any]],
    event_detail: dict[str, Any],
) -> dict[str, Any]:
    """Recompute headline metrics from serialized decisions/details only."""

    frame_metrics = compute_frame_metrics(frame_decisions)
    truth_events = event_detail["truth_events"]
    predicted_events = event_detail["predicted_events"]
    truth_count = len(truth_events)
    prediction_count = len(predicted_events)
    matched_count = sum(1 for event in truth_events if event["hit"])
    recall = _safe_div(matched_count, truth_count)
    precision = _safe_div(matched_count, prediction_count)
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    by_type = {
        event_type: {
            "truth_count": 0,
            "prediction_count": 0,
            "matched_count": 0,
        }
        for event_type in ACTIVE_EVENT_TYPES
    }
    for event in truth_events:
        by_type[event["event_type"]]["truth_count"] += 1
        if event["hit"]:
            by_type[event["event_type"]]["matched_count"] += 1
    for event in predicted_events:
        by_type[event["event_type"]]["prediction_count"] += 1
    return {
        "frame": frame_metrics,
        "event": {
            "truth_count": truth_count,
            "prediction_count": prediction_count,
            "matched_count": matched_count,
            "event_recall": recall,
            "event_precision": precision,
            "event_f1": f1,
            "by_type": by_type,
        },
    }


def flatten_frame_decisions(
    dialog_id: str,
    truth_frames: Sequence[dict[str, Any]],
    segments: Sequence[TimelineSegment],
) -> list[dict[str, Any]]:
    predicted_labels: list[str] = []
    for segment in segments:
        predicted_labels.extend(
            [segment.event_type] * (segment.end_frame - segment.start_frame + 1)
        )
    if len(predicted_labels) != len(truth_frames):
        raise ValueError(
            f"prediction timeline length {len(predicted_labels)} does not match "
            f"truth frame count {len(truth_frames)}"
        )
    decisions: list[dict[str, Any]] = []
    for frame_index, truth_row in enumerate(truth_frames):
        decisions.append(
            {
                "dialog_id": dialog_id,
                "frame_index": frame_index,
                "truth_event_id": truth_row["event_id"],
                "truth_event_type": truth_row["event_type"],
                "predicted_event_type": predicted_labels[frame_index],
                "is_speech": bool(truth_row["is_speech"]),
            }
        )
    return decisions
