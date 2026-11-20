"""Cross-stage consistency self-check with E_INCONSISTENT reporting."""

from __future__ import annotations

import math
from typing import Any, Sequence

from .catalog import EVENT_TYPE_IDS
from .codes import BusinessCode
from .errors import AedError
from .metrics import (
    aggregate_metrics,
    metrics_from_recorded_details,
)
from .postprocess import validate_timeline
from .types import TimelineSegment


def _inconsistent(
    category: str,
    message: str,
    *,
    dialog_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AedError:
    payload = {"category": category}
    if details:
        payload.update(details)
    return AedError(
        BusinessCode.E_INCONSISTENT,
        message,
        dialog_id=dialog_id,
        field=category,
        details=payload,
    )


def _close(first: float, second: float, tolerance: float = 1e-12) -> bool:
    return math.isclose(float(first), float(second), rel_tol=0.0, abs_tol=tolerance)


def _segment_from_dict(payload: dict[str, Any]) -> TimelineSegment:
    return TimelineSegment(
        start_frame=int(payload["start_frame"]),
        end_frame=int(payload["end_frame"]),
        start_ms=float(payload["start_ms"]),
        end_ms=float(payload["end_ms"]),
        event_type=payload["event_type"],
        confidence=float(payload["confidence"]),
        is_event=bool(payload["is_event"]),
        segment_id=payload["segment_id"],
    )


def _check_frame_decisions(
    dialog: dict[str, Any],
    *,
    dialog_id: str,
) -> None:
    decisions = dialog["frame_decisions"]
    if len(decisions) != dialog["frame_count"]:
        raise _inconsistent(
            "frame_index_sets",
            "frame decision count does not match dialog frame_count",
            dialog_id=dialog_id,
            details={
                "frame_decisions": len(decisions),
                "frame_count": dialog["frame_count"],
            },
        )
    indices = [decision["frame_index"] for decision in decisions]
    if indices != list(range(dialog["frame_count"])):
        raise _inconsistent(
            "frame_index_sets",
            "frame decision indices are not the canonical contiguous set",
            dialog_id=dialog_id,
        )
    for decision in decisions:
        if (
            decision["truth_event_type"] not in EVENT_TYPE_IDS
            or decision["predicted_event_type"] not in EVENT_TYPE_IDS
        ):
            raise _inconsistent(
                "frame_index_sets",
                "frame decision contains an uncontrolled event type",
                dialog_id=dialog_id,
                details={"frame_index": decision["frame_index"]},
            )


def _check_event_details(
    dialog: dict[str, Any],
    *,
    dialog_id: str,
) -> None:
    event_metrics = dialog["metrics"]["event"]
    details = event_metrics["details"]
    truth_events = details["truth_events"]
    predicted_events = details["predicted_events"]
    threshold = float(event_metrics["overlap_threshold"])
    prediction_by_id = {event["prediction_id"]: event for event in predicted_events}
    truth_by_id = {event["truth_event_id"]: event for event in truth_events}
    if len(prediction_by_id) != len(predicted_events):
        raise _inconsistent(
            "event_details",
            "prediction ids are not unique",
            dialog_id=dialog_id,
        )
    if len(truth_by_id) != len(truth_events):
        raise _inconsistent(
            "event_details",
            "truth event ids are not unique after frame reconstruction",
            dialog_id=dialog_id,
        )
    hits = 0
    for event in truth_events:
        if event["hit"]:
            hits += 1
            prediction = prediction_by_id.get(event["matched_prediction_id"])
            if (
                prediction is None
                or not prediction["hit"]
                or prediction["matched_truth_event_id"] != event["truth_event_id"]
                or float(event["iou"]) + 1e-12 < threshold
            ):
                raise _inconsistent(
                    "event_details",
                    "truth hit is not reciprocally recorded in prediction details",
                    dialog_id=dialog_id,
                    details={"truth_event_id": event["truth_event_id"]},
                )
        elif event["matched_prediction_id"] is not None:
            raise _inconsistent(
                "event_details",
                "non-hit truth event has a matched prediction",
                dialog_id=dialog_id,
                details={"truth_event_id": event["truth_event_id"]},
            )
    prediction_hits = 0
    for prediction in predicted_events:
        if prediction["hit"]:
            prediction_hits += 1
            truth = truth_by_id.get(prediction["matched_truth_event_id"])
            if (
                truth is None
                or not truth["hit"]
                or truth["matched_prediction_id"] != prediction["prediction_id"]
                or float(prediction["iou"]) + 1e-12 < threshold
            ):
                raise _inconsistent(
                    "event_details",
                    "prediction hit is not reciprocally recorded in truth details",
                    dialog_id=dialog_id,
                    details={"prediction_id": prediction["prediction_id"]},
                )
        elif prediction["matched_truth_event_id"] is not None:
            raise _inconsistent(
                "event_details",
                "non-hit prediction has a matched truth event",
                dialog_id=dialog_id,
                details={"prediction_id": prediction["prediction_id"]},
            )
    if hits != prediction_hits or hits != int(event_metrics["matched_count"]):
        raise _inconsistent(
            "event_details",
            "hit counts are not consistent between truth, prediction and summary",
            dialog_id=dialog_id,
            details={
                "truth_hits": hits,
                "prediction_hits": prediction_hits,
                "summary_matched_count": event_metrics["matched_count"],
            },
        )


def _check_dialog_metrics(
    dialog: dict[str, Any],
    *,
    dialog_id: str,
) -> None:
    recomputed = metrics_from_recorded_details(
        dialog["frame_decisions"],
        dialog["metrics"]["event"]["details"],
    )
    actual_frame = dialog["metrics"]["frame"]
    expected_frame = recomputed["frame"]
    for key in (
        "total_frames",
        "correct_frames",
        "truth_active_frames",
        "predicted_active_frames",
        "active_true_positive_frames",
    ):
        if actual_frame[key] != expected_frame[key]:
            raise _inconsistent(
                "metrics_recompute",
                f"frame metric {key} cannot be recomputed from per-frame decisions",
                dialog_id=dialog_id,
                details={"metric": key},
            )
    for key in ("frame_accuracy", "event_frame_recall", "event_frame_precision"):
        if not _close(actual_frame[key], expected_frame[key]):
            raise _inconsistent(
                "metrics_recompute",
                f"frame metric {key} cannot be recomputed from per-frame decisions",
                dialog_id=dialog_id,
                details={"metric": key},
            )
    if actual_frame["confusion"] != expected_frame["confusion"]:
        raise _inconsistent(
            "metrics_recompute",
            "frame confusion matrix cannot be recomputed from per-frame decisions",
            dialog_id=dialog_id,
        )
    if actual_frame["per_event_type"] != expected_frame["per_event_type"]:
        raise _inconsistent(
            "metrics_recompute",
            "per-event-type frame statistics cannot be recomputed",
            dialog_id=dialog_id,
        )
    actual_event = dialog["metrics"]["event"]
    expected_event = recomputed["event"]
    for key in ("truth_count", "prediction_count", "matched_count"):
        if actual_event[key] != expected_event[key]:
            raise _inconsistent(
                "metrics_recompute",
                f"event metric {key} cannot be recomputed from event details",
                dialog_id=dialog_id,
                details={"metric": key},
            )
    for key in ("event_recall", "event_precision", "event_f1"):
        if not _close(actual_event[key], expected_event[key]):
            raise _inconsistent(
                "metrics_recompute",
                f"event metric {key} cannot be recomputed from event details",
                dialog_id=dialog_id,
                details={"metric": key},
            )
    if actual_event["by_type"] != expected_event["by_type"]:
        raise _inconsistent(
            "metrics_recompute",
            "event-type confusion statistics cannot be recomputed from event details",
            dialog_id=dialog_id,
        )


def _check_dialog(
    dialog: dict[str, Any],
    *,
    report: dict[str, Any],
) -> None:
    dialog_id = dialog["dialog_id"]
    if report["path_by_dialog"].get(dialog_id) != dialog["path"]:
        raise _inconsistent(
            "path_coverage",
            "dialog path is missing from path_by_dialog or disagrees with execution",
            dialog_id=dialog_id,
        )
    checks = dialog["frame_index_checks"]
    hashes = {
        "truth": checks["truth_sha256"],
        "feature": checks["feature_sha256"],
        "detection": checks["detection_sha256"],
        "postprocess": checks["postprocess_sha256"],
    }
    if len(set(hashes.values())) != 1:
        raise _inconsistent(
            "frame_index_sets",
            "truth/feature/detection/post-process frame index sets differ",
            dialog_id=dialog_id,
            details={"hashes": hashes},
        )
    counts = {
        "truth": checks["truth_count"],
        "feature": checks["feature_count"],
        "detection": checks["detection_count"],
        "postprocess": checks["postprocess_count"],
    }
    if len(set(counts.values())) != 1 or counts["truth"] != dialog["frame_count"]:
        raise _inconsistent(
            "frame_index_sets",
            "stage frame counts differ or do not match dialog frame_count",
            dialog_id=dialog_id,
            details={"counts": counts, "frame_count": dialog["frame_count"]},
        )
    try:
        segments = [_segment_from_dict(payload) for payload in dialog["segments"]]
        validate_timeline(
            segments,
            frame_count=dialog["frame_count"],
            duration_ms=dialog["duration_ms"],
            dialog_id=dialog_id,
        )
    except AedError as exc:
        if exc.code == BusinessCode.E_INCONSISTENT.value:
            raise
        raise _inconsistent(
            "timeline_coverage",
            "timeline coverage/range validation failed during self-check",
            dialog_id=dialog_id,
            details={"source_code": exc.code, "source_details": exc.details},
        ) from exc
    _check_frame_decisions(dialog, dialog_id=dialog_id)
    _check_event_details(dialog, dialog_id=dialog_id)
    _check_dialog_metrics(dialog, dialog_id=dialog_id)


def _aggregate_record(dialog: dict[str, Any]) -> dict[str, Any]:
    return {
        "frame_decisions": dialog["frame_decisions"],
        "event": dialog["metrics"]["event"],
    }


def _compare_aggregate(
    actual: dict[str, Any],
    expected: dict[str, Any],
    *,
    category: str,
    path_name: str | None = None,
) -> None:
    for frame_key in (
        "total_frames",
        "correct_frames",
        "truth_active_frames",
        "predicted_active_frames",
        "active_true_positive_frames",
    ):
        if actual["frame"][frame_key] != expected["frame"][frame_key]:
            raise _inconsistent(
                "metrics_recompute",
                f"aggregate frame metric {frame_key} is inconsistent",
                details={"metric": frame_key, "path": path_name},
            )
    for frame_key in ("frame_accuracy", "event_frame_recall", "event_frame_precision"):
        if not _close(actual["frame"][frame_key], expected["frame"][frame_key]):
            raise _inconsistent(
                "metrics_recompute",
                f"aggregate frame metric {frame_key} is inconsistent",
                details={"metric": frame_key, "path": path_name},
            )
    for event_key in ("truth_count", "prediction_count", "matched_count"):
        if actual["event"][event_key] != expected["event"][event_key]:
            raise _inconsistent(
                "metrics_recompute",
                f"aggregate event metric {event_key} is inconsistent",
                details={"metric": event_key, "path": path_name},
            )
    for event_key in ("event_recall", "event_precision", "event_f1"):
        if not _close(actual["event"][event_key], expected["event"][event_key]):
            raise _inconsistent(
                "metrics_recompute",
                f"aggregate event metric {event_key} is inconsistent",
                details={"metric": event_key, "path": path_name},
            )
    if actual["event"]["by_type"] != expected["event"]["by_type"]:
        raise _inconsistent(
            "metrics_recompute",
            "aggregate event-type confusion statistics are inconsistent",
            details={"path": path_name},
        )


def run_self_check(report: dict[str, Any]) -> dict[str, Any]:
    """Validate frame-set equality, full timeline coverage and metric replay."""

    for dialog in report["dialogs"]:
        _check_dialog(dialog, report=report)

    if report["scale_observation"]["dialog_count"] != len(report["dialogs"]):
        raise _inconsistent(
            "path_coverage",
            "scale observation dialog_count disagrees with report dialogs",
        )
    if report["scale_observation"]["total_frames"] != sum(
        dialog["frame_count"] for dialog in report["dialogs"]
    ):
        raise _inconsistent(
            "path_coverage",
            "scale observation total_frames disagrees with report frame counts",
        )

    aggregate = aggregate_metrics([_aggregate_record(dialog) for dialog in report["dialogs"]])
    _compare_aggregate(report["metrics"], aggregate, category="metrics_recompute")
    for path_name, actual in report["metrics_by_path"].items():
        records = [
            _aggregate_record(dialog)
            for dialog in report["dialogs"]
            if dialog["path"] == path_name
        ]
        expected = aggregate_metrics(records)
        _compare_aggregate(
            actual,
            expected,
            category="metrics_recompute",
            path_name=path_name,
        )

    return {
        "status": BusinessCode.OK.value,
        "categories": {
            "frame_index_sets": "OK",
            "timeline_coverage": "OK",
            "event_details": "OK",
            "metrics_recompute": "OK",
            "path_coverage": "OK",
        },
        "dialog_count": len(report["dialogs"]),
        "total_frames": sum(dialog["frame_count"] for dialog in report["dialogs"]),
        "rule": (
            "truth/feature/detection/post-process frame index sets must be byte-identical; "
            "segments must be contiguous and cover every frame; frame and event metrics "
            "must replay from recorded decisions/details"
        ),
    }
