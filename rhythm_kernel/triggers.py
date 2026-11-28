"""Beat-cue / song-request linkage trigger sequence.

Each valid judgement emits two triggers:

* ``beat_cue`` prefers the mapped beat time and must never fire before it;
* ``song_request`` prefers the corrected click time and is also clamped to the
  attributed segment ``[start_ms, end_ms]`` window.

Triggers are ordered by segment declaration, logical time, trigger type
(``beat_cue`` before ``song_request``) and the stable event input order, so
the sequence is deterministic and reviewable.
"""

from __future__ import annotations

from .model import clamp, clone

TYPE_RANK = {"beat_cue": 0, "song_request": 1}


def build_event_triggers(result, event_order_index):
    """Build the two trigger records for one judged event, or an empty list."""
    if not result["valid"]:
        result["trigger_issues"] = [
            {
                "code": "trigger.skipped_invalid_judgment",
                "event_id": result["event_id"],
                "message": "No trigger is scheduled for an invalid judgement",
                "path": "triggers",
                "severity": "warning",
            }
        ]
        result["triggers"] = []
        return []

    segment = result["segment"]
    beat_ms = result["judgment"]["beat_ms"]
    corrected = result["corrected_click_ms"]
    start = segment["start_ms"]
    end = segment["end_ms"]

    cue_time = clamp(beat_ms, start, end)
    preferred_request = max(corrected, beat_ms)
    request_time = clamp(preferred_request, max(beat_ms, start), end)

    common = {
        "beat_ms": beat_ms,
        "channel": result["channel"],
        "event_id": result["event_id"],
        "language": result["language"],
        "listener_id": result["listener_id"],
        "requested_song_id": result["requested_song_id"],
        "segment_declared_index": segment["declared_index"],
        "segment_id": segment["id"],
    }
    triggers = [
        {
            **common,
            "clamped_to_segment_end": cue_time != beat_ms and cue_time == end,
            "clamped_to_segment_start": cue_time != beat_ms and cue_time == start,
            "not_before_beat": cue_time >= beat_ms,
            "preferred_time_ms": beat_ms,
            "trigger_id": f"{result['event_id']}:beat_cue",
            "trigger_time_ms": cue_time,
            "type": "beat_cue",
            "within_segment": start <= cue_time <= end,
        },
        {
            **common,
            "clamped_to_segment_end": request_time != corrected
            and request_time == end,
            "clamped_to_segment_start": request_time != corrected
            and request_time == start,
            "not_before_beat": request_time >= beat_ms,
            "preferred_time_ms": preferred_request,
            "trigger_id": f"{result['event_id']}:song_request",
            "trigger_time_ms": request_time,
            "type": "song_request",
            "within_segment": start <= request_time <= end,
        },
    ]
    result["trigger_issues"] = []
    result["triggers"] = triggers
    return triggers


def assemble_sequence(results, event_order):
    """Collect per-event triggers and assign stable global sequence indices."""
    order_index = {event_id: index for index, event_id in enumerate(event_order)}
    all_triggers = []
    for result in results:
        all_triggers.extend(build_event_triggers(result, order_index))

    ordered = sorted(
        all_triggers,
        key=lambda trigger: (
            trigger["segment_declared_index"],
            trigger["trigger_time_ms"],
            TYPE_RANK[trigger["type"]],
            order_index[trigger["event_id"]],
        ),
    )
    sequence = [clone(trigger) for trigger in ordered]
    for index, trigger in enumerate(sequence):
        trigger["sequence_index"] = index
    return sequence


def strip_sequence_indices(all_triggers):
    """Remove global sequence indices from per-event trigger records."""
    for trigger in all_triggers:
        trigger.pop("sequence_index", None)
