"""Per-frame measurement primitives."""

from __future__ import annotations

import math

import numpy as np

from .signal_utils import EPS, linear_to_db, longest_true_run, moving_average, safe_ratio


def _low_quantile_block_rms(values: np.ndarray, block_size: int) -> float:
    """10th percentile of short-block RMS values inside one frame."""
    n = values.size
    if n == 0:
        return 0.0
    block_size = max(4, min(int(block_size), n))
    n_blocks = n // block_size
    if n_blocks < 2:
        return float(np.sqrt(np.mean(np.square(values))))
    trimmed = values[: n_blocks * block_size].reshape(n_blocks, block_size)
    block_rms = np.sqrt(np.mean(np.square(trimmed), axis=1))
    return float(np.percentile(block_rms, 10.0))


def local_diff_reference(
    diff_energy_smoothed: np.ndarray,
    index: int,
    sample_rate: int,
    gap_ms: float = 1.5,
    window_ms: float = 12.0,
    stat_percentile: float = 80.0,
) -> float:
    """Robust local differential-energy reference from both sides of a candidate.

    The larger of two robust side envelopes is used so that steady loud music
    is not mistaken for a transient: a real click must stand out on both sides.
    A high percentile (default 80th) is important for harmonically rich or
    sawtooth-like music whose derivative energy is strongly peaked once per
    cycle; a median would make every waveform cycle look like a click.
    """
    if diff_energy_smoothed.size == 0:
        return 0.0
    gap = max(1, int(round(gap_ms * sample_rate / 1000.0)))
    width = max(4, int(round(window_ms * sample_rate / 1000.0)))
    n = diff_energy_smoothed.size
    lo = max(0, index - gap - width)
    hi = max(0, index - gap)
    left = diff_energy_smoothed[lo:hi]
    lo_r = min(n, index + gap)
    hi_r = min(n, index + gap + width)
    right = diff_energy_smoothed[lo_r:hi_r]
    envelopes = []
    if left.size >= 2:
        envelopes.append(float(np.percentile(left, stat_percentile)))
    if right.size >= 2:
        envelopes.append(float(np.percentile(right, stat_percentile)))
    if not envelopes:
        return float(np.percentile(diff_energy_smoothed, stat_percentile))
    return float(max(envelopes))


def frame_metrics(
    samples: np.ndarray,
    sample_rate: int,
    frame_index: int,
    start_sample: int,
    end_sample: int,
    complete: bool,
    plateau_level: float = 0.995,
    near_plateau_ratio: float = 0.98,
) -> dict:
    """Compute all requested per-frame descriptors for one frame."""
    y = np.asarray(samples, dtype=np.float64)
    length = int(y.size)
    peak = float(np.max(np.abs(y))) if length else 0.0
    dc = float(np.mean(y)) if length else 0.0
    centered = y - dc
    frame_rms = float(np.sqrt(np.mean(np.square(centered)))) if length else 0.0

    # Fixed 2 ms analysis blocks keep the low-energy quantile comparable across
    # different frame lengths.
    block_size = max(8, min(length, int(round(0.002 * sample_rate))))
    low_q = _low_quantile_block_rms(centered, block_size)

    hard_mask = np.abs(y) >= plateau_level
    plateau_samples = longest_true_run(hard_mask)
    near_threshold = near_plateau_ratio * peak
    near_mask = np.abs(y) >= near_threshold if peak > 0 else np.zeros(length, dtype=bool)
    near_plateau_samples = longest_true_run(near_mask)
    near_plateau_fraction = float(np.mean(near_mask)) if length else 0.0

    if length > 0:
        diff = np.diff(y, prepend=y[0])
        diff_energy = diff * diff
        diff_smoothed = moving_average(diff_energy, max(2, int(round(0.00025 * sample_rate))))
        peak_index = int(np.argmax(diff_smoothed))
        diff_energy_peak = float(diff_smoothed[peak_index])
        diff_energy_median = float(np.median(diff_smoothed))
        local_ref = local_diff_reference(diff_smoothed, peak_index, sample_rate)
        jump_ratio = math.sqrt(safe_ratio(diff_energy_peak, local_ref))
        diff_peak_amplitude = math.sqrt(max(diff_energy_peak, 0.0))
    else:
        diff_energy_peak = 0.0
        diff_energy_median = 0.0
        jump_ratio = 0.0
        diff_peak_amplitude = 0.0

    return {
        "frame_index": int(frame_index),
        "start_sample": int(start_sample),
        "end_sample": int(end_sample),
        "frame_length_samples": length,
        "complete": bool(complete),
        "peak": peak,
        "peak_dbfs": float(linear_to_db(peak)),
        "peak_ratio": float(peak),
        "plateau_samples": int(plateau_samples),
        "plateau_ratio": float(plateau_samples / length) if length else 0.0,
        "near_plateau_samples": int(near_plateau_samples),
        "near_plateau_fraction": near_plateau_fraction,
        "dc_offset": dc,
        "dc_dbfs": float(linear_to_db(abs(dc))),
        "frame_rms_dbfs": float(linear_to_db(frame_rms)),
        "low_quantile_rms": float(low_q),
        "low_quantile_dbfs": float(linear_to_db(low_q)),
        "diff_energy_peak": diff_energy_peak,
        "diff_energy_median": diff_energy_median,
        "diff_peak_amplitude": diff_peak_amplitude,
        "diff_jump_ratio": float(jump_ratio),
        "impulse_flatness": 0.0,
        "jump_duration_samples": 0,
        "clipping_label": "none",
        "impulse_candidate": False,
        "noise_floor_dbfs": None,
        "noise_floor_linear": None,
        "noise_floor_updated": False,
    }


def classify_clipping_frames(
    frames: list[dict],
    sample_rate: int,
    hard_peak_ratio: float = 0.995,
    hard_plateau_ms: float = 0.125,
    soft_peak_min: float = 0.90,
    soft_peak_max: float = 0.995,
    soft_near_plateau_ms: float = 0.35,
    soft_near_fraction_min: float = 0.005,
) -> None:
    """Label frames as hard clipping, soft limiting, or none in place."""
    hard_min = max(2, int(round(hard_plateau_ms * sample_rate / 1000.0)))
    soft_min = max(3, int(round(soft_near_plateau_ms * sample_rate / 1000.0)))
    for frame in frames:
        peak_ratio = float(frame["peak_ratio"])
        plateau = int(frame["plateau_samples"])
        near = int(frame["near_plateau_samples"])
        label = "none"
        if plateau >= hard_min and peak_ratio >= hard_peak_ratio:
            label = "hard"
        elif (
            soft_peak_min <= peak_ratio < soft_peak_max
            and near >= soft_min
            and float(frame["near_plateau_fraction"]) >= soft_near_fraction_min
        ):
            label = "soft"
        frame["clipping_label"] = label
