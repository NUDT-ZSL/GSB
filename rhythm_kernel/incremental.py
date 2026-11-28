"""Incremental recomputation with explicit affected-scope tracking.

The engine keeps the previous full result set.  When events or a song's beat
grid change locally, only the affected events are re-judged; every other
result record is reused byte-for-byte and therefore stays field-identical.

Affected scopes:

* event insert/update  -> that event id only;
* event removal        -> the removed id only (record dropped);
* song beat grid change -> every event requesting that song, reported with
  the re-covered beat index/time interval.
"""

from __future__ import annotations

from .consistency import build_consistency_report
from .model import clone
from .statistics import build_stats
from .triggers import assemble_sequence, strip_sequence_indices
from .validation import flatten_validation


def affected_events_for_song(project, song_id):
    return [
        event["id"]
        for event in project["events"]
        if event["requested_song_id"] == song_id
    ]


def beat_span(song):
    beats = song["beats_ms"]
    if not beats:
        return {"beat_index_start": None, "beat_index_end": None,
                "beat_time_start_ms": None, "beat_time_end_ms": None}
    return {
        "beat_index_start": 0,
        "beat_index_end": len(beats) - 1,
        "beat_time_start_ms": beats[0],
        "beat_time_end_ms": beats[-1],
    }


def recompute_incremental(
    engine,
    previous_report,
    changed_event_ids=(),
    removed_event_ids=(),
    changed_song_ids=(),
    reason="incremental_recompute",
):
    """Re-judge only affected events and rebuild aggregate payloads.

    Returns ``(report, scope)`` where ``scope`` documents the recomputed
    event ids and the beat intervals that were revisited.
    """
    event_ids = set(changed_event_ids)
    removed_ids = set(removed_event_ids)
    beat_spans = []
    for song_id in changed_song_ids:
        event_ids.update(affected_events_for_song(engine.project, song_id))
        beat_spans.append(
            {
                "song_id": song_id,
                **beat_span(engine.songs[song_id]),
            }
        )

    previous_by_id = {
        result["event_id"]: result for result in previous_report["results"]
    }
    untouched_hashes = {
        event_id: clone(result)
        for event_id, result in previous_by_id.items()
        if event_id not in event_ids and event_id not in removed_ids
    }

    current_events = {event["id"]: event for event in engine.project["events"]}
    results = []
    recomputed_ids = []
    for event in engine.project["events"]:
        if event["id"] in event_ids:
            results.append(engine.judge_one(event))
            recomputed_ids.append(event["id"])
        else:
            results.append(clone(untouched_hashes[event["id"]]))

    event_order = [event["id"] for event in engine.project["events"]]
    trigger_sequence = assemble_sequence(results, event_order)
    strip_sequence_indices(
        [trigger for result in results for trigger in result["triggers"]]
    )
    validation = flatten_validation(results)
    stats = build_stats(results, engine.policy)
    consistency = build_consistency_report(
        results, engine.policy, engine.legal_range
    )

    engine.state_revision += 1
    scope = {
        "reason": reason,
        "revision": engine.state_revision,
        "recomputed_event_ids": sorted(recomputed_ids),
        "removed_event_ids": sorted(removed_ids),
        "unchanged_event_ids": [
            event_id for event_id in event_order if event_id not in event_ids
        ],
        "affected_beat_ranges": beat_spans,
        "recomputed_event_count": len(recomputed_ids),
        "unchanged_event_count": len(event_order) - len(recomputed_ids),
    }
    engine.incremental_history.append(dict(scope))

    report = {
        **previous_report,
        "consistency_report": consistency,
        "event_order": event_order,
        "incremental_history": list(engine.incremental_history),
        "results": results,
        "state_revision": engine.state_revision,
        "stats": stats,
        "trigger_sequence": trigger_sequence,
        "validation": validation,
    }
    return report, scope
