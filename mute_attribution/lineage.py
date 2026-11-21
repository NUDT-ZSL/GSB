from __future__ import annotations

import math
from typing import Any

from .attribution import (
    classify_metrics,
    recompute_metrics_from_evidence,
)
from .features import CallMeta
from .store import SnapshotStore
from .util import sha256_json


REQUIRED_LINEAGE_KEYS = (
    "feature_version",
    "noise_floor_version",
    "attribution_threshold_revision",
    "snapshot_revision",
    "snapshot_version",
)


def _broken(event_id: str, detail: str, **extra: Any) -> None:
    from .errors import fail

    fail(
        "E_LINEAGE_BROKEN",
        event_id=event_id,
        detail=detail,
        **extra,
    )


def replay_event(
    store: SnapshotStore,
    event: dict[str, Any],
    call: CallMeta,
) -> dict[str, Any]:
    event_id = str(event.get("event_id", ""))
    lineage = event.get("lineage")
    if not isinstance(lineage, dict):
        _broken(event_id, "event lineage is missing")
    for key in REQUIRED_LINEAGE_KEYS:
        if lineage.get(key) is None:
            _broken(event_id, f"lineage field is missing: {key}", lineage=lineage)

    attr_version = int(lineage["snapshot_version"])
    attr_snapshot = store.load_version("attribution", attr_version)
    if attr_snapshot is None:
        _broken(
            event_id,
            "attribution snapshot referenced by lineage is missing",
            snapshot_version=attr_version,
        )
    if lineage["attribution_threshold_revision"] != attr_snapshot.config_revision:
        _broken(event_id, "attribution revision does not match snapshot")

    attr_record = attr_snapshot.record_map().get(call.call_id)
    if attr_record is None:
        _broken(event_id, "attribution record is missing from snapshot")
    record_lineage = attr_record.get("lineage", {})
    if record_lineage.get("snapshot_revision") != lineage.get("snapshot_revision"):
        _broken(event_id, "snapshot revision does not match attribution record")
    if int(attr_record.get("source_feature_version", -1)) != int(
        lineage["feature_version"]
    ):
        _broken(event_id, "feature version does not match attribution record")
    if int(attr_record.get("source_noise_version", -1)) != int(
        lineage["noise_floor_version"]
    ):
        _broken(event_id, "noise floor version does not match attribution record")

    feature_snapshot = store.load_version(
        "features", int(lineage["feature_version"])
    )
    noise_snapshot = store.load_version(
        "noise_floor", int(lineage["noise_floor_version"])
    )
    if feature_snapshot is None or noise_snapshot is None:
        _broken(event_id, "upstream snapshot referenced by lineage is missing")
    feature_record = feature_snapshot.record_map().get(call.call_id)
    noise_record = noise_snapshot.record_map().get(call.call_id)
    if feature_record is None or noise_record is None:
        _broken(event_id, "upstream call record referenced by lineage is missing")
    if (
        feature_record.get("record_checksum")
        != attr_record.get("source_feature_checksum")
    ):
        _broken(event_id, "feature checksum does not match attribution source")
    if (
        noise_record.get("record_checksum")
        != attr_record.get("source_noise_checksum")
    ):
        _broken(event_id, "noise floor checksum does not match attribution source")

    segment_map = {
        str(segment["segment_id"]): segment
        for segment in attr_record.get("silence_segments", [])
    }
    segment_ids = event.get("segment_ids")
    if not isinstance(segment_ids, list) or not segment_ids:
        _broken(event_id, "event segment list is missing")
    if sorted(segment_ids) != sorted(set(segment_ids)):
        _broken(event_id, "event segment list contains duplicates")

    attr_config = attr_snapshot.stage_config
    controlled = set(attr_config.get("controlled_labels", []))
    noise_steps = attr_record.get("noise_steps", [])
    replayed_segments: list[dict[str, Any]] = []
    labels: set[str] = set()
    deviations: list[float] = []
    for segment_id in segment_ids:
        segment = segment_map.get(str(segment_id))
        if segment is None:
            _broken(event_id, "event references a missing segment", segment_id=segment_id)
        segment_lineage = segment.get("lineage", {})
        for key in REQUIRED_LINEAGE_KEYS:
            if segment_lineage.get(key) != lineage.get(key):
                _broken(
                    event_id,
                    "segment lineage does not match event lineage",
                    segment_id=segment_id,
                    field=key,
                )
        metrics = recompute_metrics_from_evidence(
            segment.get("evidence", {}),
            int(segment["start_frame"]),
            int(segment["end_frame"]),
            noise_steps,
        )
        replay_label = classify_metrics(
            metrics, attr_config, controlled, call.call_id
        )
        stored_metrics = segment.get("metrics", {})
        if replay_label != segment.get("label"):
            _broken(
                event_id,
                "replayed label differs from stored label",
                segment_id=segment_id,
                stored_label=segment.get("label"),
                replay_label=replay_label,
            )
        if not math.isclose(
            float(metrics["deviation_db"]),
            float(stored_metrics.get("deviation_db", math.nan)),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            _broken(
                event_id,
                "replayed deviation differs from stored deviation",
                segment_id=segment_id,
                stored_deviation_db=stored_metrics.get("deviation_db"),
                replay_deviation_db=metrics["deviation_db"],
            )
        labels.add(replay_label)
        deviations.append(float(metrics["deviation_db"]))
        replayed_segments.append(
            {
                "segment_id": segment_id,
                "label": replay_label,
                "deviation_db": metrics["deviation_db"],
                "evidence_checksum": sha256_json(segment.get("evidence", {})),
            }
        )
    if labels != {event.get("label")}:
        _broken(
            event_id,
            "event label is not reproducible from its segments",
            event_label=event.get("label"),
            replay_labels=sorted(labels),
        )
    replay_deviation = round(
        sum(deviations) / len(deviations), 12
    )
    if not math.isclose(
        replay_deviation,
        float(event.get("deviation_db", math.nan)),
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        _broken(
            event_id,
            "event deviation is not reproducible from its segments",
            event_deviation_db=event.get("deviation_db"),
            replay_deviation_db=replay_deviation,
        )
    return {
        "code": "OK",
        "event_id": event_id,
        "call_id": call.call_id,
        "label": event.get("label"),
        "deviation_db": replay_deviation,
        "lineage": lineage,
        "replayed_segments": replayed_segments,
    }


def check_partition(
    recomputed_calls: list[str],
    reused_calls: list[str],
    affected_calls: list[str],
) -> None:
    from .errors import fail

    recomputed = set(recomputed_calls)
    reused = set(reused_calls)
    affected = set(affected_calls)
    if recomputed & reused:
        fail(
            "E_INCONSISTENT",
            detail="reuse and recompute sets overlap",
            overlap=sorted(recomputed & reused),
        )
    if not affected.issubset(recomputed | reused):
        fail(
            "E_INCONSISTENT",
            detail="reuse/recompute union does not cover every affected call",
            missing=sorted(affected - (recomputed | reused)),
        )

