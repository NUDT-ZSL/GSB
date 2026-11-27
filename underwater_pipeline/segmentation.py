"""Turbidity interval detection and piecewise-constant shot fitting."""

from __future__ import annotations

import numpy as np

from .cuts import median_smooth
from .visibility import visibility_grade


def _binary_close(mask: np.ndarray, max_gap: int) -> np.ndarray:
    result = mask.copy()
    count = result.size
    index = 0
    while index < count:
        if result[index]:
            index += 1
            continue
        start = index
        while index < count and not result[index]:
            index += 1
        end = index
        if start > 0 and end < count and end - start <= max_gap:
            result[start:end] = True
    return result


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    index = 0
    while index < mask.size:
        if not mask[index]:
            index += 1
            continue
        start = index
        while index < mask.size and mask[index]:
            index += 1
        runs.append((start, index))
    return runs


def detect_turbidity_intervals(
    frame_scores: np.ndarray,
    timestamps: np.ndarray,
    config,
) -> tuple[np.ndarray, list[dict]]:
    smoothed = median_smooth(frame_scores, config.frame_smooth_width)
    reference = float(
        np.percentile(smoothed, config.turbidity_reference_percentile)
    )
    low_mask = smoothed < reference - config.turbidity_drop_points
    low_mask = _binary_close(low_mask, config.turbidity_gap_fill)

    intervals: list[dict] = []
    for start, end in _runs(low_mask):
        if end - start < config.min_turbidity_frames:
            continue

        # Propagate across the short attack/recovery ramps.
        refined_start = start
        while (
            refined_start > 0
            and start - refined_start < 12
            and smoothed[refined_start - 1] > smoothed[refined_start] + 0.10
        ):
            refined_start -= 1
        refined_end = end
        while (
            refined_end < smoothed.size
            and refined_end - end < 12
            and smoothed[refined_end] > smoothed[refined_end - 1] + 0.10
        ):
            refined_end += 1

        inside = smoothed[refined_start:refined_end]
        before = smoothed[max(0, refined_start - 8) : refined_start]
        after = smoothed[refined_end : min(smoothed.size, refined_end + 8)]
        outside = np.concatenate([before, after])
        if outside.size == 0:
            outside = np.asarray([reference])
        outside_reference = float(np.median(outside))
        score = float(inside.mean())
        level, level_name = visibility_grade(score, config)
        drop = outside_reference - score
        if drop < max(5.0, config.turbidity_drop_points * 0.42):
            continue
        intervals.append(
            {
                "start_frame": int(refined_start),
                "end_frame_exclusive": int(refined_end),
                "start_time_s": round(float(timestamps[refined_start]), 6),
                "end_time_s": round(float(timestamps[refined_end - 1]), 6),
                "recovery_time_s": round(
                    float(
                        timestamps[
                            min(refined_end, timestamps.size - 1)
                        ]
                    ),
                    6,
                ),
                "frame_count": int(refined_end - refined_start),
                "mean_visibility_score": round(score, 6),
                "visibility_drop": round(float(drop), 6),
                "outside_reference_score": round(outside_reference, 6),
                "turbidity_level": level,
                "turbidity_level_name": level_name,
            }
        )
    return smoothed, intervals


def fit_piecewise_constant(
    shot_scores: list[float],
    shot_ids: list[str],
    penalty: float,
    max_adjacent_change: float | None = None,
) -> list[dict]:
    """Exact least-squares segmentation with a fixed per-segment penalty."""

    values = np.asarray(shot_scores, dtype=np.float64)
    count = values.size
    if count == 0:
        return []
    prefix = np.concatenate([[0.0], np.cumsum(values)])
    prefix_sq = np.concatenate([[0.0], np.cumsum(values * values)])
    infinity = float("inf")
    best = np.full(count + 1, infinity, dtype=np.float64)
    previous = np.full(count + 1, -1, dtype=np.int32)
    best[0] = 0.0

    for end in range(1, count + 1):
        for start in range(end):
            length = end - start
            if max_adjacent_change is not None and length > 1:
                differences = np.abs(np.diff(values[start:end]))
                if np.any(differences > max_adjacent_change):
                    continue
            total = prefix[end] - prefix[start]
            total_sq = prefix_sq[end] - prefix_sq[start]
            sse = total_sq - total * total / length
            sse = max(0.0, float(sse))
            candidate = best[start] + sse + penalty
            if candidate < best[end] - 1e-12:
                best[end] = candidate
                previous[end] = start

    ranges: list[tuple[int, int]] = []
    end = count
    while end > 0:
        start = int(previous[end])
        ranges.append((start, end))
        end = start
    ranges.reverse()

    segments: list[dict] = []
    for start, end in ranges:
        segment_values = values[start:end]
        fitted = float(segment_values.mean())
        segments.append(
            {
                "shot_start_index": int(start),
                "shot_end_index_exclusive": int(end),
                "shot_ids": shot_ids[start:end],
                "fitted_visibility_score": round(fitted, 6),
                "segment_sse": round(
                    float(np.sum((segment_values - fitted) ** 2)), 6
                ),
            }
        )
    return segments
