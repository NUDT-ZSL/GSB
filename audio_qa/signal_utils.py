"""Small NumPy-only signal helpers used by the analyzer and generator."""

from __future__ import annotations

import math

import numpy as np


EPS = 1.0e-12


def linear_to_db(value: float | np.ndarray, floor: float = EPS):
    """Convert a linear amplitude to dBFS, clipping the argument at ``floor``."""
    return 20.0 * np.log10(np.maximum(value, floor))


def db_to_linear(value_db: float) -> float:
    return 10.0 ** (value_db / 20.0)


def longest_true_run(mask: np.ndarray) -> int:
    """Return the longest run of True values in a one-dimensional mask."""
    if mask.size == 0 or not bool(mask.any()):
        return 0
    padded = np.concatenate((np.zeros(1, dtype=bool), mask.astype(bool), np.zeros(1, dtype=bool)))
    changes = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1)
    if starts.size == 0:
        return 0
    return int(np.max(ends - starts))


def moving_average(values: np.ndarray, width: int) -> np.ndarray:
    """Centered moving average without external dependencies."""
    width = max(1, int(width))
    if width == 1 or values.size == 0:
        return values.astype(np.float64, copy=True)
    if values.size < width:
        return np.full(values.shape, float(np.mean(values)), dtype=np.float64)
    kernel = np.ones(width, dtype=np.float64) / float(width)
    padded = np.pad(values.astype(np.float64), (width // 2, width - 1 - width // 2), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def dilate_mask(mask: np.ndarray, radius: int) -> np.ndarray:
    """Dilate a boolean mask by ``radius`` samples on both sides."""
    radius = max(0, int(radius))
    if radius == 0 or mask.size == 0:
        return mask.astype(bool, copy=True)
    values = mask.astype(np.int32)
    cumulative = np.concatenate((np.zeros(1, dtype=np.int32), np.cumsum(values)))
    n = values.size
    idx = np.arange(n)
    lo = np.maximum(0, idx - radius)
    hi = np.minimum(n, idx + radius + 1)
    return (cumulative[hi] - cumulative[lo]) > 0


def true_runs(mask: np.ndarray, max_gap: int = 0, min_length: int = 1) -> list[tuple[int, int]]:
    """Convert a boolean mask into half-open runs, merging gaps and dropping short runs."""
    if mask.size == 0:
        return []
    padded = np.concatenate((np.zeros(1, dtype=bool), mask.astype(bool), np.zeros(1, dtype=bool)))
    changes = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1)
    if starts.size == 0:
        return []
    runs = [[int(s), int(e)] for s, e in zip(starts, ends)]
    if max_gap > 0:
        merged: list[list[int]] = []
        for run in runs:
            if merged and run[0] - merged[-1][1] <= max_gap:
                merged[-1][1] = run[1]
            else:
                merged.append(run)
        runs = merged
    return [(s, e) for s, e in runs if e - s >= min_length]


def interval_duration(intervals: list[dict]) -> int:
    return int(sum(int(item["end_sample"]) - int(item["start_sample"]) for item in intervals))


def union_length(intervals: list[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    ordered = sorted(intervals)
    total = 0
    cur_s, cur_e = ordered[0]
    for s, e in ordered[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    total += cur_e - cur_s
    return int(total)


def overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def percentile(values: np.ndarray, q: float) -> float:
    if values.size == 0:
        return float("nan")
    return float(np.percentile(values, q))


def safe_ratio(numerator: float, denominator: float, floor: float = EPS) -> float:
    return float(numerator / max(denominator, floor))


def ms_to_samples(ms: float, sample_rate: int) -> int:
    return max(1, int(round(float(ms) * sample_rate / 1000.0)))


def samples_to_ms(samples: int, sample_rate: int) -> float:
    return 1000.0 * float(samples) / float(sample_rate)


def db_gain(amplitude: float) -> float:
    return float(20.0 * math.log10(max(amplitude, EPS)))
