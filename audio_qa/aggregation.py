"""Aggregate frame decisions into sample intervals."""

from __future__ import annotations

import numpy as np


def _frame_groups(indices: np.ndarray, max_gap_frames: int, min_frames: int) -> list[tuple[int, int]]:
    if indices.size == 0:
        return []
    groups: list[list[int]] = []
    for value in indices:
        idx = int(value)
        if groups and idx - groups[-1][1] <= max_gap_frames:
            groups[-1][1] = idx
        else:
            groups.append([idx, idx])
    return [(s, e) for s, e in groups if e - s + 1 >= min_frames]


def aggregate_clipping(
    frames: list[dict],
    hop: int,
    sample_rate: int,
    segment_start: int,
    segment_end: int,
    max_gap_frames: int = 1,
    min_frames: int = 2,
) -> list[dict]:
    indices = np.asarray(
        [int(f["frame_index"]) for f in frames if f["clipping_label"] != "none"], dtype=np.int64
    )
    groups = _frame_groups(indices, max_gap_frames=max_gap_frames, min_frames=min_frames)
    duration = max(1, int(segment_end) - int(segment_start))
    bands: list[dict] = []
    for first, last in groups:
        subset = frames[first : last + 1]
        flagged = [f for f in subset if f["clipping_label"] != "none"]
        labels = {str(f["clipping_label"]) for f in flagged}
        subtype = "hard" if labels == {"hard"} else "soft" if labels == {"soft"} else "mixed"
        start_sample = int(subset[0]["start_sample"])
        end_sample = int(subset[-1]["end_sample"])
        coverage = len(flagged) * hop / float(duration)
        bands.append(
            {
                "type": "clipping",
                "subtype": subtype,
                "start_sample": start_sample,
                "end_sample": end_sample,
                "duration_samples": end_sample - start_sample,
                "duration_seconds": (end_sample - start_sample) / float(sample_rate),
                "segment_coverage": float(coverage),
                "clipped_frame_count": len(flagged),
                "frame_count": len(subset),
                "clip_frame_ratio": len(flagged) / float(len(subset)),
                "peak_ratio_max": float(max(f["peak_ratio"] for f in flagged)),
                "plateau_samples_max": int(max(f["plateau_samples"] for f in flagged)),
                "plateau_ratio_max": float(max(f["plateau_ratio"] for f in flagged)),
                "hard_frame_count": sum(1 for f in flagged if f["clipping_label"] == "hard"),
                "soft_frame_count": sum(1 for f in flagged if f["clipping_label"] == "soft"),
            }
        )
    return bands


def aggregate_dc_offset(
    frames: list[dict],
    hop: int,
    sample_rate: int,
    segment_start: int,
    segment_end: int,
    threshold: float = 0.015,
    max_gap_frames: int = 2,
    min_frames: int = 5,
) -> list[dict]:
    # Clipping/limiting distorts the frame mean.  Hold the last valid DC
    # estimate through clipped frames instead of letting the flat top create a
    # spurious DC band.
    near_clip = np.zeros(len(frames), dtype=bool)
    for index, frame in enumerate(frames):
        if frame["clipping_label"] == "none":
            continue
        lo = max(0, index - 2)
        hi = min(len(frames), index + 3)
        near_clip[lo:hi] = True
    effective_dc: list[float] = []
    held = 0.0
    have_held = False
    for index, frame in enumerate(frames):
        value = float(frame["dc_offset"])
        if frame["clipping_label"] == "none" and not near_clip[index]:
            held = value
            have_held = True
            effective_dc.append(value)
        elif have_held:
            effective_dc.append(held)
        else:
            effective_dc.append(0.0)
    indices = np.asarray(
        [int(f["frame_index"]) for f, value in zip(frames, effective_dc) if abs(value) >= threshold],
        dtype=np.int64,
    )
    groups = _frame_groups(indices, max_gap_frames=max_gap_frames, min_frames=min_frames)
    duration = max(1, int(segment_end) - int(segment_start))
    bands: list[dict] = []
    for first, last in groups:
        subset = frames[first : last + 1]
        values = np.asarray(effective_dc[first : last + 1], dtype=np.float64)
        median_dc = float(np.median(values))
        if abs(median_dc) < threshold:
            continue
        start_sample = int(subset[0]["start_sample"])
        end_sample = int(subset[-1]["end_sample"])
        bands.append(
            {
                "type": "dc_offset",
                "start_sample": start_sample,
                "end_sample": end_sample,
                "duration_samples": end_sample - start_sample,
                "duration_seconds": (end_sample - start_sample) / float(sample_rate),
                "segment_coverage": (end_sample - start_sample) / float(duration),
                "median_dc_offset": median_dc,
                "abs_dc_offset": abs(median_dc),
                "polarity": "positive" if median_dc >= 0 else "negative",
                "min_dc_offset": float(np.min(values)),
                "max_dc_offset": float(np.max(values)),
                "frame_count": len(subset),
                "above_threshold_fraction": float(np.mean(np.abs(values) >= threshold)),
            }
        )
    return bands


def build_clipping_suppress_mask(
    frames: list[dict], signal: np.ndarray, offset: int, sample_rate: int
) -> np.ndarray:
    """Sample mask over actually saturated samples, dilated to suppress edges."""
    signal = np.asarray(signal, dtype=np.float64)
    mask = np.zeros(signal.size, dtype=bool)
    for frame in frames:
        if frame["clipping_label"] == "none":
            continue
        lo = max(0, int(frame["start_sample"]) - offset)
        hi = min(signal.size, int(frame["end_sample"]) - offset)
        if hi > lo:
            threshold = 0.99 if frame["clipping_label"] == "hard" else 0.86
            mask[lo:hi] |= np.abs(signal[lo:hi]) >= threshold
    guard = max(1, int(round(0.003 * sample_rate)))
    if mask.any():
        values = mask.astype(np.int32)
        cumulative = np.concatenate((np.zeros(1, dtype=np.int32), np.cumsum(values)))
        n = values.size
        idx = np.arange(n)
        lo = np.maximum(0, idx - guard)
        hi = np.minimum(n, idx + guard + 1)
        mask = (cumulative[hi] - cumulative[lo]) > 0
    return mask
