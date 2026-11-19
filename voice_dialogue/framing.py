"""Deterministic sample-accurate frame boundaries.

Frames are left-aligned. ``frame_ms`` determines the window length and
``hop_ms`` the shift between consecutive frames; both are converted to sample
indices with exact integer rounding so the same PCM always yields the same
frame count and boundaries.
"""

from __future__ import annotations

import numpy as np

from .errors import BusinessError, ErrorCode


def ms_to_samples(ms: float, sample_rate: int) -> int:
    """Convert milliseconds to an exact integer sample count."""
    return int(round(ms * sample_rate / 1000.0))


def frame_count(num_samples: int, frame_size: int, hop_size: int) -> int:
    """Number of left-aligned frames fitting inside ``num_samples``."""
    if frame_size <= 0 or hop_size <= 0:
        raise BusinessError(
            ErrorCode.E_CONFIG_INVALID,
            "frame_size and hop_size must be positive",
            field="frame_ms",
        )
    if num_samples < frame_size:
        return 0
    return int((num_samples - frame_size) // hop_size) + 1


def frame_starts(num_samples: int, frame_size: int, hop_size: int) -> np.ndarray:
    """Return the inclusive start sample index of every frame."""
    n = frame_count(num_samples, frame_size, hop_size)
    return np.arange(n, dtype=np.int64) * hop_size


def iter_frames(pcm: np.ndarray, frame_size: int, hop_size: int):
    """Yield ``(frame_index, start_sample, window)`` frame by frame.

    Streaming iteration keeps peak resident memory independent of total length:
    only one window is materialised at a time.
    """
    n = frame_count(pcm.shape[0], frame_size, hop_size)
    for idx in range(n):
        start = idx * hop_size
        yield idx, start, pcm[start:start + frame_size]
