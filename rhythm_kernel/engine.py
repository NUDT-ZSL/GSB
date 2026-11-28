"""Deterministic judging engine orchestrating the full pipeline."""

from __future__ import annotations

import json

from .correction import (
    build_channel_corrections,
    channel_report,
    describe_correction,
    empty_correction,
)
from .judgment import (
    judge_event,
    judgement_without_click,
    mark_invalid,
    unknown_song_judgement,
)
from .model import ALGORITHM_VERSION, SCHEMA_VERSION, TRACE, clone, group_key
from .triggers import assemble_sequence, strip_sequence_indices
from .validation import flatten_validation, validate_event


def load_project(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


class RhythmEngine:
    """Holds an immutable project view and produces reviewable judgements."""

    def __init__(self, project):
        self.project = clone(project)
        self.policy = self.project["policy"]
        self.legal_range = self.project["legal_time_range_ms"]
        self.songs = {song["id"]: song for song in self.project["songs"]}
        self.timeline = list(self.project["timeline"])
        self.profiles, self.profile_order = build_channel_corrections(
            self.project["channels"]
        )
        self.state_revision = 0
        self.incremental_history = []

    # ------------------------------------------------------------------ core
    def judge_one(self, event):
        """Produce the full per-event result record for one input event."""
        key = group_key(event["language"], event["channel"])
        profile = self.profiles.get(key)
        if profile is not None:
            correction = describe_correction(profile)
            correction_ms = correction["applied_correction_ms"]
            corrected = event["click_ms"] - correction_ms
        else:
            correction = empty_correction(event["language"], event["channel"])
            correction_ms = None
            corrected = None

        song = self.songs.get(event["requested_song_id"])
        if song is None:
            judgement = unknown_song_judgement(
                event["requested_song_id"], self.policy
            )
        elif corrected is None:
            judgement = judgement_without_click(song, self.policy)
        else:
            judgement = judge_event(song, corrected, self.policy)

        issues, segment_block, has_error = validate_event(
            event,
            self.legal_range,
            corrected,
            profile is not None,
            correction,
            song,
            judgement,
            self.timeline,
        )

        if has_error:
            mark_invalid(judgement)

        result = {
            "abs_deviation_ms": judgement["abs_deviation_ms"],
            "beat_index": judgement["beat_index"],
            "beat_ms": judgement["beat_ms"],
            "beat_number": judgement["beat_number"],
            "channel": event["channel"],
            "corrected_click_ms": corrected,
            "correction": correction,
            "correction_ms": correction_ms,
            "deviation_ms": judgement["deviation_ms"],
            "event_id": event["id"],
            "grade": judgement["grade"],
            "input": clone(event),
            "judgment": judgement,
            "language": event["language"],
            "listener_id": event["listener_id"],
            "raw_click_ms": event["click_ms"],
            "requested_song_id": event["requested_song_id"],
            "segment": segment_block,
            "status": judgement["status"],
            "submitted_at_ms": event["submitted_at_ms"],
            "trace": self._trace(),
            "trigger_issues": [],
            "triggers": [],
            "valid": judgement["valid"],
            "validation": {"issues": issues, "valid": not has_error},
        }
        return result

    def run_full(self):
        """Recompute every event and rebuild all aggregate payloads."""
        event_order = [event["id"] for event in self.project["events"]]
        results = [self.judge_one(event) for event in self.project["events"]]
        trigger_sequence = assemble_sequence(results, event_order)
        strip_sequence_indices(
            [trigger for result in results for trigger in result["triggers"]]
        )
        validation = flatten_validation(results)
        stats = self.build_stats(results)
        consistency_report = self.build_consistency(results)
        return {
            "algorithm_version": ALGORITHM_VERSION,
            "consistency_report": consistency_report,
            "corrections_by_channel": channel_report(
                self.profiles, self.profile_order
            ),
            "event_order": event_order,
            "incremental_history": list(self.incremental_history),
            "legal_time_range_ms": [
                self.legal_range["start_ms"],
                self.legal_range["end_ms"],
            ],
            "policy": clone(self.policy),
            "project_id": self.project["project_id"],
            "results": results,
            "schema_version": SCHEMA_VERSION,
            "state_revision": self.state_revision,
            "stats": stats,
            "trigger_sequence": trigger_sequence,
            "validation": validation,
        }

    # ------------------------------------------------------------- statistics
    def build_stats(self, results):
        from .statistics import build_stats

        return build_stats(results, self.policy)

    def build_consistency(self, results):
        from .consistency import build_consistency_report

        return build_consistency_report(results, self.policy, self.legal_range)

    def _trace(self):
        trace = dict(TRACE)
        trace["legal_time_range_ms"] = [
            self.legal_range["start_ms"],
            self.legal_range["end_ms"],
        ]
        return trace

    # ------------------------------------------------------------- mutation
    def upsert_event(self, event):
        """Insert or replace one input event for incremental processing."""
        events = self.project["events"]
        for index, existing in enumerate(events):
            if existing["id"] == event["id"]:
                events[index] = clone(event)
                break
        else:
            events.append(clone(event))

    def update_song_beats(self, song_id, beats_ms, bpm=None):
        """Locally replace a song's beat grid (and optionally its BPM)."""
        song = self.songs[song_id]
        song["beats_ms"] = list(beats_ms)
        if bpm is not None:
            song["bpm"] = bpm
