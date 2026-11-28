"""Project validation, segment attribution and issue ordering.

Issues are produced per event in a fixed emission order; the flattened
top-level validation list then sorts errors before warnings and uses the
issue code as the deterministic tie-breaker.
"""

from __future__ import annotations


def issue(code, severity, message, path, **extra):
    record = {"code": code, "severity": severity, "message": message, "path": path}
    for key in sorted(extra):
        record[key] = extra[key]
    return record


def find_segment(timeline, time_ms):
    """First declared segment whose inclusive window contains ``time_ms``."""
    if time_ms is None:
        return None
    for index, segment in enumerate(timeline):
        if segment["start_ms"] <= time_ms <= segment["end_ms"]:
            return index, segment
    return None


def describe_segment(index, segment, attribution):
    return {
        "attribution": attribution,
        "declared_index": index,
        "end_ms": segment["end_ms"],
        "id": segment["id"],
        "name": segment["name"],
        "song_id": segment["song_id"],
        "start_ms": segment["start_ms"],
    }


def validate_event(
    event,
    legal_range,
    corrected_click_ms,
    correction_known,
    profile,
    song,
    judgment,
    timeline,
):
    """Run all range/reference checks and attribute a timeline segment.

    Returns ``(issues, segment_block, validation_error)`` where a validation
    error means the event cannot be published as a valid judgement.
    """
    issues = []
    start, end = legal_range["start_ms"], legal_range["end_ms"]

    if not (start <= event["submitted_at_ms"] <= end):
        issues.append(
            issue(
                "event.submitted_at_out_of_legal_range",
                "error",
                "Submitted timestamp is outside the declared legal time range",
                "submitted_at_ms",
                event_id=event["id"],
                legal_range_ms=[start, end],
                value=event["submitted_at_ms"],
            )
        )

    if not (start <= event["click_ms"] <= end):
        issues.append(
            issue(
                "event.click_out_of_legal_range",
                "error",
                "Raw click timestamp is outside the declared legal time range",
                "click_ms",
                event_id=event["id"],
                legal_range_ms=[start, end],
                value=event["click_ms"],
            )
        )

    if corrected_click_ms is not None and not (
        start <= corrected_click_ms <= end
    ):
        issues.append(
            issue(
                "event.corrected_click_out_of_legal_range",
                "error",
                "Corrected click timestamp is outside the declared legal time range",
                "corrected_click_ms",
                correction_ms=profile["one_way_correction_ms"],
                event_id=event["id"],
                legal_range_ms=[start, end],
                value=corrected_click_ms,
            )
        )

    if not correction_known:
        issues.append(
            issue(
                "event.unknown_channel_group",
                "error",
                "No RTT profile exists for this language/channel pair",
                "event.language+channel",
                channel=event["channel"],
                event_id=event["id"],
                group_key=f"{event['language']}|{event['channel']}",
                language=event["language"],
            )
        )

    if song is None:
        issues.append(
            issue(
                "event.unknown_song",
                "error",
                "Requested song does not exist in the project",
                "requested_song_id",
                event_id=event["id"],
                song_id=event["requested_song_id"],
            )
        )

    segment_block = None
    click_match = (
        find_segment(timeline, corrected_click_ms)
        if corrected_click_ms is not None
        else None
    )
    beat_match = (
        find_segment(timeline, judgment.get("beat_ms"))
        if judgment.get("beat_ms") is not None
        else None
    )

    if click_match is not None:
        index, segment = click_match
        segment_block = describe_segment(index, segment, "corrected_click")
    elif beat_match is not None:
        index, segment = beat_match
        segment_block = describe_segment(index, segment, "mapped_beat")
        issues.append(
            issue(
                "event.segment_from_mapped_beat",
                "warning",
                "Corrected click is outside all segments; mapped beat supplies the segment",
                "segment",
                event_id=event["id"],
                segment_id=segment["id"],
            )
        )
    else:
        issues.append(
            issue(
                "event.no_timeline_segment",
                "error",
                "No program segment contains the corrected click or mapped beat",
                "timeline",
                corrected_click_ms=corrected_click_ms,
                event_id=event["id"],
                mapped_beat_ms=judgment.get("beat_ms"),
            )
        )

    if song is None and segment_block is not None:
        issues.append(
            issue(
                "event.song_segment_mismatch",
                "warning",
                "Requested song differs from the attributed segment song",
                "requested_song_id",
                event_id=event["id"],
                requested_song_id=event["requested_song_id"],
                segment_id=segment_block["id"],
                segment_song_id=segment_block["song_id"],
            )
        )

    has_error = any(item["severity"] == "error" for item in issues)
    return issues, segment_block, has_error


def flatten_validation(results):
    """Flatten per-event issues into the deterministic top-level list."""
    issues = []
    for result in results:
        issues.extend(result["validation"]["issues"])
    severity_rank = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda item: (severity_rank[item["severity"]], item["code"]))
    return issues
