"""Frame-level acoustic features with exact sample-index framing."""

from __future__ import annotations

import numpy as np

from .errors import ResourceLimitError

FEATURE_BYTES_PER_FRAME = 16  # energy + zcr as float64


def frame_count(num_samples, frame_len, hop):
    if num_samples < frame_len:
        return 0
    return 1 + (num_samples - frame_len) // hop


def frame_bounds(index, frame_len, hop):
    """Exact sample-index bounds [start, end) of frame `index`."""
    start = index * hop
    return start, start + frame_len


def check_frame_memory(num_samples, frame_len, hop, limits):
    """Return (hop, degradation|None); enforce the frame-feature memory cap."""
    needed = frame_count(num_samples, frame_len, hop) * FEATURE_BYTES_PER_FRAME
    if needed <= limits.max_frame_feature_bytes:
        return hop, None
    if limits.on_frame_memory_exceeded == "strict":
        raise ResourceLimitError(
            "frame feature memory limit exceeded",
            limit="max_frame_feature_bytes",
            needed_bytes=needed,
            allowed_bytes=limits.max_frame_feature_bytes,
        )
    new_hop = hop
    while needed > limits.max_frame_feature_bytes:
        new_hop *= 2
        needed = frame_count(num_samples, frame_len, new_hop) * FEATURE_BYTES_PER_FRAME
    return new_hop, {
        "category": "frame_hop_restricted",
        "requested_hop": hop,
        "applied_hop": new_hop,
        "allowed_bytes": limits.max_frame_feature_bytes,
    }


def compute_features(pcm, frame_len, hop):
    """Short-time energy and zero-crossing rate per frame.

    Frame i covers samples [i*hop, i*hop + frame_len).  Energy is the mean
    square of the int16 signal normalized to [-1, 1]; ZCR is the fraction of
    adjacent sign changes inside the frame.
    """
    pcm = np.asarray(pcm, dtype=np.int16)
    n = frame_count(pcm.size, frame_len, hop)
    if n == 0:
        return np.zeros(0), np.zeros(0)
    x = pcm.astype(np.float64) / 32768.0
    frames = np.lib.stride_tricks.as_strided(
        x,
        shape=(n, frame_len),
        strides=(x.strides[0] * hop, x.strides[0]),
    )
    energy = np.mean(frames * frames, axis=1)
    signs = np.signbit(frames)
    zcr = np.mean(signs[:, 1:] != signs[:, :-1], axis=1)
    return energy, zcr
