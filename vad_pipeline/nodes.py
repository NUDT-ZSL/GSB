"""Pure, deterministic compute nodes of the VAD pipeline.

Node order: features -> noise -> decision -> segments. Each function takes
only its declared inputs and returns a JSON-serializable artifact dict, so the
same inputs always yield byte-identical artifacts after codec.dumps().
"""

from __future__ import annotations

import numpy as np

from . import errors

FEATURES_VERSION = "features/1"
NOISE_VERSION = "noise/1"
DECISION_VERSION = "decision/1"
SEGMENTS_VERSION = "segments/1"


def compute_features(pcm_i16, sample_rate, frame_ms, hop_ms):
    """Short-time energy + zero-crossing rate per frame.

    Frame boundaries are exact sample indexes: frame i covers
    [i*hop, min(i*hop + frame, n_samples)).
    """
    frame = sample_rate * frame_ms // 1000
    hop = sample_rate * hop_ms // 1000
    n = len(pcm_i16)
    n_frames = (n + hop - 1) // hop
    x = pcm_i16.astype(np.float64) / 32768.0
    energy = np.zeros(n_frames, dtype=np.float64)
    zcr = np.zeros(n_frames, dtype=np.float64)
    for i in range(n_frames):
        start = i * hop
        seg = x[start : min(start + frame, n)]
        if seg.size == 0:
            continue
        energy[i] = float(np.mean(seg * seg))
        if seg.size > 1:
            signs = np.signbit(seg)
            zcr[i] = float(np.count_nonzero(signs[1:] != signs[:-1])) / (seg.size - 1)
    return {
        "n_frames": int(n_frames),
        "frame_ms": frame_ms,
        "hop_ms": hop_ms,
        "sample_rate": sample_rate,
        "matrix": np.column_stack([energy, zcr]),
    }


def compute_noise(energy, inactive_frames, percentile, percentile_window):
    """Deterministic noise-floor estimate over inactive frames.

    Inactive frames are sorted by energy; a window of ``percentile_window``
    frames centered on the requested percentile rank is selected, and the
    estimate is the mean energy of exactly those frames. The used frame index
    set is stored sorted so the scalar can be replayed bit-exactly.
    """
    if not (0.0 < percentile <= 100.0):
        raise errors.VadError(
            errors.E_PERCENTILE_INVALID,
            "percentile must be in (0, 100]",
            percentile=percentile,
        )
    if percentile_window < 1:
        raise errors.VadError(
            errors.E_PERCENTILE_INVALID,
            "percentile_window must be >= 1",
            percentile_window=percentile_window,
        )
    idx = np.asarray(sorted(int(i) for i in inactive_frames), dtype=np.int64)
    if idx.size < percentile_window:
        raise errors.VadError(
            errors.E_NOISE_UNESTIMABLE,
            "not enough inactive frames to estimate the noise floor",
            available=int(idx.size),
            required=int(percentile_window),
        )
    order = np.argsort(energy[idx], kind="stable")
    sorted_idx = idx[order]
    rank = int(np.floor((percentile / 100.0) * (idx.size - 1)))
    half = percentile_window // 2
    lo = min(max(0, rank - half), idx.size - percentile_window)
    used = sorted(int(u) for u in sorted_idx[lo : lo + percentile_window])
    noise = float(np.mean(energy[np.asarray(used, dtype=np.int64)]))
    return {
        "noise": noise,
        "used_frames": used,
        "percentile": float(percentile),
        "percentile_window": int(percentile_window),
    }


def replay_noise_scalar(energy, used_frames):
    """Replay the noise scalar from its recorded frame index set."""
    return float(np.mean(energy[np.asarray(used_frames, dtype=np.int64)]))


def compute_decision(energy, noise, mult_low, mult_high):
    """Dual-threshold hysteresis: enter speech at the low threshold, exit at
    the high threshold (energy falling below it)."""
    low = float(noise) * float(mult_low)
    high = float(noise) * float(mult_high)
    n = len(energy)
    decisions = np.zeros(n, dtype=np.bool_)
    in_speech = False
    for i in range(n):
        e = float(energy[i])
        if not in_speech and e >= low:
            in_speech = True
        elif in_speech and e < high:
            in_speech = False
        decisions[i] = in_speech
    return {
        "decisions": decisions,
        "low_threshold": low,
        "high_threshold": high,
    }


def validate_segments(segments, duration_ms):
    prev_end = 0
    for idx, seg in enumerate(segments):
        start, end = seg
        if start < prev_end:
            raise errors.VadError(
                errors.E_OVERLAP,
                "segments must be ascending and non-overlapping",
                segment_index=idx,
                start_ms=start,
                prev_end_ms=prev_end,
            )
        if start < 0 or end > duration_ms or end < start:
            raise errors.VadError(
                errors.E_OUT_OF_RANGE,
                "segment lies outside the clip duration",
                segment_index=idx,
                start_ms=start,
                end_ms=end,
                duration_ms=duration_ms,
            )
        prev_end = end


def compute_segments(
    decisions, frame_ms, hop_ms, duration_ms, min_speech_ms, min_silence_ms
):
    """Merge frame decisions into a millisecond segment list."""
    n = len(decisions)
    runs = []
    i = 0
    while i < n:
        if decisions[i]:
            j = i
            while j + 1 < n and decisions[j + 1]:
                j += 1
            runs.append([i * hop_ms, min(j * hop_ms + frame_ms, duration_ms)])
            i = j + 1
        else:
            i += 1
    merged = []
    for seg in runs:
        if merged and seg[0] - merged[-1][1] < min_silence_ms:
            merged[-1][1] = seg[1]
        else:
            merged.append(list(seg))
    segments = [s for s in merged if s[1] - s[0] >= min_speech_ms]
    validate_segments(segments, duration_ms)
    return {
        "segments": segments,
        "min_speech_ms": min_speech_ms,
        "min_silence_ms": min_silence_ms,
    }
