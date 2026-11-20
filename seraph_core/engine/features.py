"""Deterministic framing, frame-level acoustics and segment pooling.

All numeric operations run in float64 and are expressed through fixed-shape
NumPy array programmes: no threading-dependent reductions, no dictionary
order dependence and no wall-clock reads.  Given the same PCM bytes and the
same configuration the outputs are bit identical run after run.
"""

from __future__ import annotations

import math

import numpy as np


def frame_signal(pcm: np.ndarray, frame_length: int,
                 hop_length: int) -> np.ndarray:
    """Split ``pcm`` into overlapping frames using exact sample boundaries."""
    n_frames = 1 + max(0, (pcm.shape[0] - frame_length) // hop_length)
    if n_frames == 0:
        return np.empty((0, frame_length), dtype=np.float64)
    starts = np.arange(n_frames, dtype=np.int64) * hop_length
    offsets = np.arange(frame_length, dtype=np.int64)
    return pcm[starts[:, None] + offsets[None, :]].astype(np.float64, copy=True)


def f0_candidate(frame: np.ndarray, sample_rate: int,
                 f0_min: float = 70.0, f0_max: float = 300.0) -> float:
    """Autocorrelation F0 candidate; deterministic argmax on ties (first max)."""
    n = frame.shape[0]
    if n < 2:
        return 0.0
    centred = frame - frame.mean()
    denom = float(np.dot(centred, centred))
    if denom <= 0.0:
        return 0.0
    lag_min = max(1, int(math.floor(sample_rate / f0_max)))
    lag_max = min(n - 1, int(math.ceil(sample_rate / f0_min)))
    if lag_max <= lag_min:
        return 0.0
    acf = np.empty(lag_max + 1, dtype=np.float64)
    for lag in range(lag_min, lag_max + 1):
        acf[lag] = float(np.dot(centred[:-lag], centred[lag:]) / denom)
    best_lag = int(np.argmax(acf[lag_min:lag_max + 1])) + lag_min
    best_corr = float(acf[best_lag])
    if not best_corr > 0.3:
        return 0.0
    return float(sample_rate) / float(best_lag)


def frame_features(frames: np.ndarray, sample_rate: int) -> np.ndarray:
    """Return per-frame [energy, zcr, f0, mean_abs, rms_derivative] (float64)."""
    n_frames = frames.shape[0]
    out = np.zeros((n_frames, 5), dtype=np.float64)
    for idx in range(n_frames):
        frame = frames[idx]
        energy = float(np.mean(frame * frame))
        zcr = float(np.mean(np.abs(np.diff(np.signbit(frame).astype(np.int8))) > 0))
        f0 = f0_candidate(frame, sample_rate)
        mean_abs = float(np.mean(np.abs(frame)))
        rms_deriv = float(np.sqrt(np.mean(np.diff(frame) ** 2))) if frame.size > 1 else 0.0
        out[idx] = (energy, zcr, f0, mean_abs, rms_deriv)
    return out


def detect_silence(features: np.ndarray, config) -> np.ndarray:
    """Boolean silence mask based on the energy column and its z-score."""
    energy = features[:, 0]
    mean = float(np.mean(energy))
    std = float(np.std(energy))
    if std <= 0.0:
        return np.zeros(features.shape[0], dtype=bool)
    threshold = mean - config.silence_z_threshold * std
    return energy < threshold


def segment_boundaries(is_speech: np.ndarray, min_silence_frames: int
                       ) -> list[tuple[int, int]]:
    """Group speech frames into segments separated by silence runs.

    A silence run shorter than ``min_silence_frames`` does not split the
    segment, matching the configured dialogue cut rule exactly.
    """
    boundaries: list[tuple[int, int]] = []
    n = is_speech.shape[0]
    idx = 0
    while idx < n:
        if not is_speech[idx]:
            idx += 1
            continue
        start = idx
        silence_run = 0
        last_speech = idx
        split = False
        while idx < n:
            if is_speech[idx]:
                silence_run = 0
                last_speech = idx
                idx += 1
            else:
                silence_run += 1
                if silence_run >= min_silence_frames:
                    split = True
                    break
                idx += 1
        if split or idx >= n:
            boundaries.append((start, last_speech + 1))
    return boundaries


POOL_STATS = ("mean", "std", "max")


def pool_segment(features: np.ndarray, start: int, end: int) -> np.ndarray:
    """Pool a frame range into a deterministic segment feature vector."""
    block = features[start:end]
    parts = [block.mean(axis=0), block.std(axis=0), block.max(axis=0)]
    extra = np.array([
        float(end - start),
        float(np.mean(block[:, 0] > 0.0)),
    ], dtype=np.float64)
    return np.concatenate(parts + [extra]).astype(np.float64, copy=True)


def segment_width(n_frame_features: int) -> int:
    return n_frame_features * len(POOL_STATS) + 2


def standardize_fit(matrix: np.ndarray, subnormal_protection: bool
                    ) -> tuple[np.ndarray, np.ndarray]:
    """Return (mean, std) statistics computed in float64 over all segments."""
    mean = matrix.mean(axis=0).astype(np.float64)
    std = matrix.std(axis=0).astype(np.float64)
    tiny = np.finfo(np.float64).tiny
    floor = tiny * 8.0 if subnormal_protection else 0.0
    std = np.where(std > floor, std, 1.0)
    return mean, std


def standardize_apply(matrix: np.ndarray, mean: np.ndarray, std: np.ndarray,
                      feature_scale: float) -> np.ndarray:
    scaled = ((matrix - mean) / std) * feature_scale
    return np.asarray(scaled, dtype=np.float64)
