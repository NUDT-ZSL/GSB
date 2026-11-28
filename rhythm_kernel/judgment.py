"""Beat-grid windows and per-event judgement construction."""

from __future__ import annotations

from .model import (
    RULES,
    beat_interval_ms,
    grade_for,
    nearest_beat,
    window_bounds,
)


def build_windows(song, policy):
    """Full window payload for a song's BPM.

    ``None`` is returned for unknown songs where no beat interval exists.
    """
    if song is None:
        return None
    ratios = dict(policy["window_ratios"])
    interval = beat_interval_ms(song["bpm"])
    boundaries = window_bounds(interval, ratios)
    return {
        "beat_interval_ms": interval,
        "boundaries_ms": dict(boundaries),
        "inclusive": True,
        "ranges_ms": {
            name: {"lower_ms": -value, "upper_ms": value}
            for name, value in boundaries.items()
        },
        "ratios": ratios,
    }


def judge_event(song, corrected_click_ms, policy):
    """Map a corrected click onto the nearest beat and grade the deviation.

    Returns a judgement payload plus the attributed segment-independent beat
    information.  ``song is None`` models an unknown requested song.
    """
    if song is None:
        return unknown_song_judgement(None, policy)
    if corrected_click_ms is None:
        return judgement_without_click(song, policy)

    windows = build_windows(song, policy)
    interval = windows["beat_interval_ms"]
    index, beat_ms = nearest_beat(song["beats_ms"], corrected_click_ms)
    deviation = corrected_click_ms - beat_ms
    abs_deviation = abs(deviation)
    candidate = grade_for(abs_deviation, windows["boundaries_ms"])
    return {
        "abs_deviation_ms": abs_deviation,
        "beat_index": index,
        "beat_interval_ms": interval,
        "beat_ms": beat_ms,
        "beat_number": index + 1,
        "bpm": song["bpm"],
        "candidate_grade": candidate,
        "deviation_ms": deviation,
        "grade": candidate,
        "rules": dict(RULES),
        "song_id": song["id"],
        "status": "JUDGED",
        "valid": True,
        "windows_ms": windows,
    }


def unknown_song_judgement(song_id, policy):
    """Judgement block for a requested song absent from the project."""
    return {
        "abs_deviation_ms": None,
        "beat_index": None,
        "beat_interval_ms": None,
        "beat_ms": None,
        "beat_number": None,
        "bpm": None,
        "candidate_grade": None,
        "deviation_ms": None,
        "grade": None,
        "rules": dict(RULES),
        "song_id": song_id,
        "status": "INVALID",
        "valid": False,
        "windows_ms": None,
    }


def judgement_without_click(song, policy):
    """Known song but no correctable click (missing RTT profile)."""
    windows = build_windows(song, policy)
    return {
        "abs_deviation_ms": None,
        "beat_index": None,
        "beat_interval_ms": windows["beat_interval_ms"],
        "beat_ms": None,
        "beat_number": None,
        "bpm": song["bpm"],
        "candidate_grade": None,
        "deviation_ms": None,
        "grade": None,
        "rules": dict(RULES),
        "song_id": song["id"],
        "status": "INVALID",
        "valid": False,
        "windows_ms": windows,
    }


def mark_invalid(judgement):
    """Null only the public grade; beat mapping stays reviewable.

    Events that still map onto a beat (e.g. out-of-legal-range clicks) keep
    their candidate grade, deviation and beat fields for post-mortem analysis.
    """
    judgement["valid"] = False
    judgement["status"] = "INVALID"
    judgement["grade"] = None
    return judgement
