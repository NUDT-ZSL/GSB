"""Raw shot-boundary filtering and short-shot stabilization."""

from __future__ import annotations

from typing import Any

import numpy as np


def median_smooth(values: np.ndarray, width: int) -> np.ndarray:
    width = max(1, int(width))
    if width <= 1:
        return values.astype(np.float64, copy=True)
    radius = width // 2
    padded = np.pad(values.astype(np.float64), radius, mode="edge")
    result = np.empty_like(values, dtype=np.float64)
    for index in range(values.size):
        result[index] = np.median(padded[index : index + width])
    return result


def _sample_duration(timestamps: np.ndarray, start: int, end: int) -> float:
    if end <= start:
        return 0.0
    if timestamps.size > 1:
        interval = float(np.median(np.diff(timestamps)))
    else:
        interval = 0.0
    if end < timestamps.size:
        return float(timestamps[end] - timestamps[start])
    return float(timestamps[-1] - timestamps[start] + interval)


def filter_turbidity_collisions(
    candidates: list[dict],
    smoothed_scores: np.ndarray,
    turbidity_intervals: list[dict],
    config,
) -> tuple[list[dict], list[dict]]:
    """Remove raw cut peaks that are better explained by visibility change."""

    transition_frames: dict[int, list[str]] = {}
    for interval in turbidity_intervals:
        transition_frames.setdefault(int(interval["start_frame"]), []).append(
            "turbidity_influx"
        )
        end = int(interval["end_frame_exclusive"])
        if end < smoothed_scores.size:
            transition_frames.setdefault(end, []).append("turbidity_recovery")

    kept: list[dict] = []
    arbitration: list[dict] = []
    for candidate in candidates:
        frame = int(candidate["frame_index"])
        nearby_hits: list[tuple[int, str]] = []
        radius = max(1, int(config.turbidity_event_radius))
        for offset in range(-radius, radius + 1):
            for event_type in transition_frames.get(frame + offset, []):
                nearby_hits.append((frame + offset, event_type))
        if not nearby_hits:
            kept.append(candidate)
            continue
        nearby_types = [event_type for _, event_type in nearby_hits]
        nearest_frame, nearest_type = min(
            nearby_hits, key=lambda item: (abs(item[0] - frame), item[0])
        )

        before_start = max(0, frame - 4)
        after_end = min(smoothed_scores.size, frame + 4)
        before = float(np.median(smoothed_scores[before_start:frame]))
        after = float(np.median(smoothed_scores[frame:after_end]))
        jump = after - before
        cut_evidence = float(candidate["boundary_metric"])
        visibility_evidence = abs(jump) / max(
            config.visibility_evidence_scale, 1e-9
        )
        turbidity_wins = (
            visibility_evidence
            > cut_evidence * config.mixed_event_visibility_bias
        )
        record = {
            "frame_index": frame,
            "time_s": None,
            "turbidity_event_types": sorted(set(nearby_types)),
            "nearest_turbidity_transition_frame": int(nearest_frame),
            "nearest_turbidity_event_type": nearest_type,
            "cut_evidence": round(cut_evidence, 6),
            "cut_threshold_ratio": round(
                float(candidate["strength"]), 6
            ),
            "visibility_evidence": round(visibility_evidence, 6),
            "visibility_jump": round(jump, 6),
            "decision": "turbidity_change" if turbidity_wins else "shot_cut",
        }
        arbitration.append(record)
        if turbidity_wins:
            candidate = dict(candidate)
            candidate["filtered_as"] = "turbidity_change"
            candidate["visibility_jump"] = jump
        else:
            kept.append(candidate)

    return kept, arbitration


def stabilize_cuts(
    raw_candidates: list[dict],
    n_frames: int,
    timestamps: np.ndarray,
    config,
    pre_removed_frames: list[int] | None = None,
) -> dict[str, Any]:
    """Drop one-frame false shots and merge all too-short fragments."""

    strengths = {
        int(item["frame_index"]): float(item["strength"])
        for item in raw_candidates
    }
    cuts = set(strengths)
    removals: list[dict] = []
    for frame in sorted(set(int(value) for value in (pre_removed_frames or []))):
        if frame not in cuts:
            continue
        cuts.remove(frame)
        removals.append(
            {
                "frame_index": frame,
                "frame_number": frame + 1,
                "time_s": round(float(timestamps[frame]), 6),
                "reason": "turbidity_change_filtered",
                "segment_start_frame": None,
                "segment_end_frame_exclusive": None,
                "segment_frames": None,
                "segment_seconds": None,
                "raw_strength": round(float(strengths.get(frame, 0.0)), 6),
            }
        )

    while True:
        boundaries = [0] + sorted(cuts) + [n_frames]
        failing: tuple[int, int] | None = None
        for index in range(len(boundaries) - 1):
            start = boundaries[index]
            end = boundaries[index + 1]
            frames = end - start
            seconds = _sample_duration(timestamps, start, end)
            if frames < config.min_shot_frames or seconds < config.min_shot_seconds:
                failing = (start, end)
                break
        if failing is None:
            break

        start, end = failing
        frames = end - start
        seconds = _sample_duration(timestamps, start, end)
        start_cut = start if start > 0 else None
        end_cut = end if end < n_frames else None

        to_remove: list[int] = []
        reason = "short_segment_merged"
        if frames <= 1 and start_cut is not None and end_cut is not None:
            to_remove = [start_cut, end_cut]
            reason = "single_frame_segment_removed"
        elif start_cut is None:
            to_remove = [end_cut] if end_cut is not None else []
            reason = "short_leading_segment_merged"
        elif end_cut is None:
            to_remove = [start_cut]
            reason = "short_trailing_segment_merged"
        else:
            left_strength = strengths.get(start_cut, 0.0)
            right_strength = strengths.get(end_cut, 0.0)
            if left_strength <= right_strength:
                to_remove = [start_cut]
                reason = "short_segment_merged_left"
            else:
                to_remove = [end_cut]
                reason = "short_segment_merged_right"

        if not to_remove:
            break
        for cut in to_remove:
            if cut in cuts:
                cuts.remove(cut)
                removals.append(
                    {
                        "frame_index": int(cut),
                        "frame_number": int(cut + 1),
                        "time_s": round(float(timestamps[cut]), 6),
                        "reason": reason,
                        "segment_start_frame": int(start),
                        "segment_end_frame_exclusive": int(end),
                        "segment_frames": int(frames),
                        "segment_seconds": round(float(seconds), 6),
                        "raw_strength": round(float(strengths.get(cut, 0.0)), 6),
                    }
                )

    final_frames = sorted(cuts)
    raw_frames = sorted(strengths)
    retained = [value for value in raw_frames if value in cuts]
    changes: list[dict] = []
    removed_lookup = {item["frame_index"]: item for item in removals}
    for frame in raw_frames:
        if frame in removed_lookup:
            changes.append(
                {
                    "frame_index": frame,
                    "frame_number": frame + 1,
                    "time_s": round(float(timestamps[frame]), 6),
                    "change": "removed",
                    "reason": removed_lookup[frame]["reason"],
                }
            )
        else:
            changes.append(
                {
                    "frame_index": frame,
                    "frame_number": frame + 1,
                    "time_s": round(float(timestamps[frame]), 6),
                    "change": "retained",
                    "reason": "meets_minimum_shot_duration",
                }
            )

    return {
        "raw_transition_frames": raw_frames,
        "stable_transition_frames": final_frames,
        "retained_transition_frames": retained,
        "removals": removals,
        "correction_changes": changes,
        "summary": {
            "raw_count": len(raw_frames),
            "stable_count": len(final_frames),
            "removed_count": len(removals),
            "retained_count": len(retained),
        },
        "parameters": {
            "min_shot_frames": config.min_shot_frames,
            "min_shot_seconds": config.min_shot_seconds,
        },
    }


def build_shot_ranges(
    cuts: list[int],
    timestamps: np.ndarray,
) -> list[dict]:
    n_frames = int(timestamps.size)
    boundaries = [0] + sorted(set(int(value) for value in cuts)) + [n_frames]
    shots: list[dict] = []
    for index in range(len(boundaries) - 1):
        start = boundaries[index]
        end = boundaries[index + 1]
        shots.append(
            {
                "shot_id": f"S{index + 1:02d}",
                "start_frame": int(start),
                "end_frame_exclusive": int(end),
                "frame_count": int(end - start),
                "start_time_s": round(float(timestamps[start]), 6),
                "end_time_s": round(float(timestamps[end - 1]), 6),
                "duration_s": round(
                    _sample_duration(timestamps, start, end), 6
                ),
            }
        )
    return shots
