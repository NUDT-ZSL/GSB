"""Independent reference judge.

This module deliberately re-implements the judging math with standalone
functions instead of reusing the production pipeline helpers.  Acceptance
tests compare the kernel output against this independent implementation inside
the project's declared float tolerance.
"""

from __future__ import annotations


def _median(values):
    data = sorted(values)
    count = len(data)
    pivot = count // 2
    if count % 2 == 1:
        return data[pivot]
    return 0.5 * (data[pivot - 1] + data[pivot])


def reference_corrections(channels):
    table = {}
    for entry in channels:
        median_rtt = _median(entry["rtt_samples_ms"])
        table[(entry["language"], entry["channel"])] = 0.5 * median_rtt
    return table


def reference_nearest_beat(beats, moment):
    best_index = 0
    best_gap = abs(beats[0] - moment)
    for index in range(1, len(beats)):
        gap = abs(beats[index] - moment)
        if gap < best_gap:
            best_gap = gap
            best_index = index
    return best_index, beats[best_index]


def reference_grade(distance, interval, ratios):
    if distance <= interval * ratios["perfect"]:
        return "PERFECT"
    if distance <= interval * ratios["good"]:
        return "GOOD"
    if distance <= interval * ratios["offset"]:
        return "OFFSET"
    return "MISS"


def reference_segment(timeline, moment):
    if moment is None:
        return None
    for index, segment in enumerate(timeline):
        if segment["start_ms"] <= moment <= segment["end_ms"]:
            return index, segment
    return None


def reference_judge(project):
    """Return ``{event_id: reference_fact}`` for every project event."""
    corrections = reference_corrections(project["channels"])
    songs = {song["id"]: song for song in project["songs"]}
    ratios = project["policy"]["window_ratios"]
    facts = {}
    for event in project["events"]:
        key = (event["language"], event["channel"])
        one_way = corrections.get(key)
        corrected = (
            event["click_ms"] - one_way if one_way is not None else None
        )
        song = songs.get(event["requested_song_id"])
        fact = {
            "correction_ms": one_way,
            "corrected_click_ms": corrected,
            "beat_ms": None,
            "beat_index": None,
            "grade": None,
            "deviation_ms": None,
            "valid": True,
        }
        if one_way is None or song is None:
            fact["valid"] = False
            facts[event["id"]] = fact
            continue

        legal = project["legal_time_range_ms"]
        if not (
            legal["start_ms"] <= event["click_ms"] <= legal["end_ms"]
            and legal["start_ms"] <= corrected <= legal["end_ms"]
            and legal["start_ms"]
            <= event["submitted_at_ms"]
            <= legal["end_ms"]
        ):
            fact["valid"] = False

        index, beat = reference_nearest_beat(song["beats_ms"], corrected)
        interval = 60000.0 / song["bpm"]
        deviation = corrected - beat
        fact.update(
            {
                "beat_ms": beat,
                "beat_index": index,
                "grade": reference_grade(abs(deviation), interval, ratios),
                "deviation_ms": deviation,
            }
        )
        facts[event["id"]] = fact
    return facts


def compare_with_kernel(report, project, tolerance_ms=None):
    """Compare each kernel result against the reference facts."""
    tolerance = (
        project["policy"]["float_tolerance_ms"]
        if tolerance_ms is None
        else tolerance_ms
    )
    facts = reference_judge(project)
    mismatches = []
    for result in report["results"]:
        fact = facts[result["event_id"]]
        if result["valid"] != fact["valid"]:
            mismatches.append(
                {
                    "event_id": result["event_id"],
                    "field": "valid",
                    "kernel": result["valid"],
                    "reference": fact["valid"],
                }
            )
            continue
        if not fact["valid"]:
            continue
        for field in ("correction_ms", "corrected_click_ms", "deviation_ms"):
            if abs(result[field] - fact[field]) > tolerance:
                mismatches.append(
                    {
                        "event_id": result["event_id"],
                        "field": field,
                        "kernel": result[field],
                        "reference": fact[field],
                    }
                )
        if result["beat_index"] != fact["beat_index"]:
            mismatches.append(
                {
                    "event_id": result["event_id"],
                    "field": "beat_index",
                    "kernel": result["beat_index"],
                    "reference": fact["beat_index"],
                }
            )
        if result["grade"] != fact["grade"]:
            mismatches.append(
                {
                    "event_id": result["event_id"],
                    "field": "grade",
                    "kernel": result["grade"],
                    "reference": fact["grade"],
                }
            )
    return {
        "checked_event_count": len(report["results"]),
        "tolerance_ms": tolerance,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "ok": not mismatches,
    }
