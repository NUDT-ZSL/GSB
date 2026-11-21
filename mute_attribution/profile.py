from __future__ import annotations

from typing import Any

import numpy as np

from .config import section_revision
from .errors import fail
from .features import CallMeta
from .util import seal_record, sha256_json


def _speech_ranges(attr_record: dict[str, Any]) -> list[tuple[int, int]]:
    return [
        (int(item["start_frame"]), int(item["end_frame"]))
        for item in attr_record.get("speech_ranges", [])
    ]


def _gap_has_speech(
    start: int, end: int, speech_ranges: list[tuple[int, int]]
) -> bool:
    return any(left < end and right > start for left, right in speech_ranges)


def derive_events_from_attribution(
    call: CallMeta,
    attr_record: dict[str, Any],
    profile_config: dict[str, Any],
) -> list[dict[str, Any]]:
    segments = sorted(
        attr_record.get("silence_segments", []),
        key=lambda item: (int(item["start_frame"]), int(item["end_frame"])),
    )
    min_gap_frames = int(profile_config.get("min_gap_frames", 0))
    speech_ranges = _speech_ranges(attr_record)
    groups: list[list[dict[str, Any]]] = []
    for segment in segments:
        if not groups:
            groups.append([segment])
            continue
        previous = groups[-1][-1]
        gap_start = int(previous["end_frame"])
        gap_end = int(segment["start_frame"])
        if (
            segment["label"] == previous["label"]
            and gap_end - gap_start <= min_gap_frames
            and not _gap_has_speech(gap_start, gap_end, speech_ranges)
        ):
            groups[-1].append(segment)
        else:
            groups.append([segment])

    events: list[dict[str, Any]] = []
    call_frame_limit = call.duration_ms // call.frame_ms
    for index, group in enumerate(groups):
        start_frame = int(group[0]["start_frame"])
        end_frame = int(group[-1]["end_frame"])
        if (
            start_frame < 0
            or end_frame <= start_frame
            or end_frame > call_frame_limit
        ):
            fail(
                "E_EVENT_RANGE",
                call_id=call.call_id,
                event_id=f"{call.call_id}-E{index + 1:03d}",
                start_frame=start_frame,
                end_frame=end_frame,
                call_frames=call_frame_limit,
            )
        deviations = [
            float(segment["metrics"]["deviation_db"]) for segment in group
        ]
        volatilities = [
            float(segment["metrics"]["volatility"]) for segment in group
        ]
        lineages = [segment.get("lineage", {}) for segment in group]
        primary_lineage = dict(lineages[0])
        primary_lineage["source_segment_lineages"] = [
            {
                "segment_id": segment["segment_id"],
                "snapshot_version": segment.get("lineage", {}).get(
                    "snapshot_version"
                ),
            }
            for segment in group
        ]
        events.append(
            {
                "event_id": f"{call.call_id}-E{index + 1:03d}",
                "call_id": call.call_id,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "start_ms": start_frame * call.frame_ms,
                "end_ms": end_frame * call.frame_ms,
                "duration_ms": (end_frame - start_frame) * call.frame_ms,
                "label": group[0]["label"],
                "segment_ids": [segment["segment_id"] for segment in group],
                "deviation_db": round(float(np.mean(deviations)), 12),
                "max_volatility": round(max(volatilities), 12),
                "lineage": primary_lineage,
            }
        )
    validate_events(events, call)
    return events


def validate_events(events: list[dict[str, Any]], call: CallMeta) -> None:
    previous_end = -1
    previous_id: str | None = None
    for event in events:
        event_id = str(event.get("event_id", ""))
        start = int(event.get("start_frame", -1))
        end = int(event.get("end_frame", -1))
        if (
            start < 0
            or end <= start
            or end * call.frame_ms > call.duration_ms
        ):
            fail(
                "E_EVENT_RANGE",
                call_id=call.call_id,
                event_id=event_id,
                start_frame=start,
                end_frame=end,
                call_frames=call.duration_ms // call.frame_ms,
            )
        if start < previous_end:
            fail(
                "E_EVENT_OVERLAP",
                call_id=call.call_id,
                event_id=event_id,
                previous_event_id=previous_id,
                previous_end=previous_end,
                start_frame=start,
            )
        previous_end = end
        previous_id = event_id


def recompute_profile_stats(
    events: list[dict[str, Any]],
    controlled_labels: list[str],
) -> dict[str, Any]:
    totals = {label: 0 for label in controlled_labels}
    counts = {label: 0 for label in controlled_labels}
    for event in events:
        label = event["label"]
        if label not in totals:
            fail("E_LABEL_INVALID", label=label, event_id=event.get("event_id"))
        totals[label] += int(event["duration_ms"])
        counts[label] += 1
    return {
        "category_totals_ms": totals,
        "category_event_counts": counts,
        "event_count": len(events),
        "total_silence_ms": sum(totals.values()),
    }


def compute_profile_record(
    call: CallMeta,
    attr_record: dict[str, Any],
    config: dict[str, Any],
    version: int,
) -> dict[str, Any]:
    controlled_labels = list(config["attribution"]["controlled_labels"])
    events = derive_events_from_attribution(call, attr_record, config["profile"])
    stats = recompute_profile_stats(events, controlled_labels)
    noise_steps = [
        dict(step) for step in attr_record.get("noise_steps", [])
    ]
    profile_config_revision = section_revision(config, "profile")
    record: dict[str, Any] = {
        "call_id": call.call_id,
        "profile_version": int(version),
        "profile_config_revision": profile_config_revision,
        "source_attribution_version": int(attr_record["attribution_version"]),
        "source_attribution_checksum": attr_record["record_checksum"],
        "duration_ms": call.duration_ms,
        "frame_ms": call.frame_ms,
        "controlled_labels": controlled_labels,
        "events": events,
        "noise_steps": noise_steps,
        "step_count": len(noise_steps),
        **stats,
        "events_checksum": sha256_json(events),
    }
    record["dependencies"] = {
        "source_attribution_checksum": attr_record["record_checksum"],
        "profile_config_revision": profile_config_revision,
        "duration_ms": call.duration_ms,
        "controlled_labels": controlled_labels,
    }
    return seal_record(record)


def validate_profile_record(
    call: CallMeta,
    attr_record: dict[str, Any],
    record: dict[str, Any],
    profile_config: dict[str, Any],
    controlled_labels: list[str],
) -> None:
    expected_events = derive_events_from_attribution(call, attr_record, profile_config)
    if record.get("events") != expected_events:
        fail(
            "E_INCONSISTENT",
            stage="profile",
            call_id=call.call_id,
            detail="event timeline is not reproducible from segment details",
        )
    stats = recompute_profile_stats(record.get("events", []), controlled_labels)
    for key, value in stats.items():
        if record.get(key) != value:
            fail(
                "E_INCONSISTENT",
                stage="profile",
                call_id=call.call_id,
                detail=f"profile statistic mismatch: {key}",
            )
    if record.get("step_count") != len(record.get("noise_steps", [])):
        fail(
            "E_INCONSISTENT",
            stage="profile",
            call_id=call.call_id,
            detail="step count mismatch",
        )

