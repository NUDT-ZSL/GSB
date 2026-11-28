"""Adaptive low-quantile noise-floor tracking and noise-band aggregation."""

from __future__ import annotations

from collections import deque

import numpy as np


def track_noise_floor(
    frames: list[dict],
    hop: int,
    sample_rate: int,
    window_seconds: float = 0.50,
    history_percentile: float = 5.0,
    smoothing: float = 0.50,
) -> None:
    """Track a noise floor with clipping/impulse-gated adaptive updates.

    Each frame contributes its own 10th-percentile short-block RMS.  A robust
    low percentile of the recent valid observations suppresses musical notes,
    while the exponential smoother follows genuine floor changes.  Frames
    marked as clipped or impulsive never update the history.
    """
    window_frames = max(5, int(round(window_seconds * sample_rate / float(hop))))
    history: deque[tuple[int, float]] = deque()
    floor_db: float | None = None
    for frame in frames:
        observation = float(frame["low_quantile_dbfs"])
        valid = bool(frame["complete"]) and frame["clipping_label"] == "none" and not frame["impulse_candidate"]
        if valid:
            history.append((int(frame["frame_index"]), observation))
            oldest = int(frame["frame_index"]) - window_frames
            while history and history[0][0] < oldest:
                history.popleft()
            values = np.fromiter((item[1] for item in history), dtype=np.float64, count=len(history))
            raw = float(np.percentile(values, history_percentile))
            if floor_db is None:
                floor_db = raw
            else:
                floor_db = smoothing * floor_db + (1.0 - smoothing) * raw
            frame["noise_floor_updated"] = True
        if floor_db is None:
            floor_db = observation
        frame["noise_floor_dbfs"] = float(floor_db)
        frame["noise_floor_linear"] = float(10.0 ** (floor_db / 20.0))


def detect_noise_bands(
    frames: list[dict],
    hop: int,
    sample_rate: int,
    segment_start: int,
    segment_end: int,
    relative_db: float = 6.0,
    absolute_fallback_dbfs: float = -66.0,
    merge_gap_ms: float = 100.0,
    min_duration_ms: float = 800.0,
) -> tuple[list[dict], dict]:
    """Aggregate elevated adaptive-floor frames into noise bands."""
    if not frames:
        return [], {"baseline_dbfs": None, "threshold_dbfs": None, "fallback_used": False}
    values = np.asarray([float(f["noise_floor_dbfs"]) for f in frames], dtype=np.float64)
    valid_values = np.asarray(
        [float(f["noise_floor_dbfs"]) for f in frames if f["noise_floor_updated"]], dtype=np.float64
    )
    basis = valid_values if valid_values.size else values
    baseline = float(np.percentile(basis, 10.0))
    threshold = baseline + relative_db
    candidate = values > threshold
    gap_frames = max(0, int(round(merge_gap_ms * sample_rate / 1000.0 / float(hop))))
    min_frames = max(1, int(round(min_duration_ms * sample_rate / 1000.0 / float(hop))))

    def groups_from_mask(mask: np.ndarray):
        padded = np.concatenate((np.zeros(1, dtype=bool), mask.astype(bool), np.zeros(1, dtype=bool)))
        changes = np.diff(padded.astype(np.int8))
        starts = np.flatnonzero(changes == 1)
        ends = np.flatnonzero(changes == -1)
        merged: list[list[int]] = []
        for s, e in zip(starts, ends):
            if merged and s - merged[-1][1] <= gap_frames:
                merged[-1][1] = int(e)
            else:
                merged.append([int(s), int(e)])
        return [(s, e) for s, e in merged if e - s >= min_frames]

    groups = groups_from_mask(candidate)
    fallback_used = False
    if not groups:
        median_floor = float(np.median(values))
        if median_floor > absolute_fallback_dbfs:
            groups = [(0, len(frames))]
            fallback_used = True

    bands: list[dict] = []
    duration = max(1, int(segment_end) - int(segment_start))
    for first, last in groups:
        subset = frames[first:last]
        band_values = np.asarray([float(f["noise_floor_dbfs"]) for f in subset], dtype=np.float64)
        start_sample = int(subset[0]["start_sample"])
        end_sample = int(subset[-1]["end_sample"])
        median_floor = float(np.median(band_values))
        bands.append(
            {
                "type": "noise",
                "start_sample": start_sample,
                "end_sample": end_sample,
                "duration_samples": end_sample - start_sample,
                "duration_seconds": (end_sample - start_sample) / float(sample_rate),
                "segment_coverage": (end_sample - start_sample) / float(duration),
                "median_noise_floor_dbfs": median_floor,
                "min_noise_floor_dbfs": float(np.min(band_values)),
                "max_noise_floor_dbfs": float(np.max(band_values)),
                "baseline_dbfs": baseline,
                "elevation_db": median_floor - baseline,
                "frame_count": len(subset),
                "updated_frame_fraction": float(np.mean([bool(f["noise_floor_updated"]) for f in subset])),
                "fallback_used": bool(fallback_used),
            }
        )
    return bands, {
        "baseline_dbfs": baseline,
        "threshold_dbfs": threshold,
        "absolute_fallback_dbfs": absolute_fallback_dbfs,
        "fallback_used": fallback_used,
    }
