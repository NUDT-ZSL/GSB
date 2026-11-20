"""Exact-sample frame extraction and deterministic feature computation."""

from __future__ import annotations

from typing import Any

import numpy as np

from .canonical import content_hash, quantize
from .config import PipelineConfig
from .errors import fail

FEATURE_NAMES = (
    "short_time_energy",
    "zero_crossing_rate",
    "spectral_centroid_hz",
    "spectral_rolloff_hz",
)


def feature_product(
    dialog_id: str,
    pcm: np.ndarray,
    config: PipelineConfig,
    truth_frame_count: int,
) -> dict[str, Any]:
    config.validate()
    signal = np.asarray(pcm, dtype=np.float64) / 32768.0
    frame_samples = config.frame_samples()
    hop_samples = config.frame_shift_samples()
    total_samples = len(signal)
    if total_samples < frame_samples:
        frame_count = 0
        frames = np.empty((0, frame_samples), dtype=np.float64)
    else:
        frame_count = 1 + (total_samples - frame_samples) // hop_samples
        frames = np.lib.stride_tricks.sliding_window_view(signal, frame_samples)[
            ::hop_samples
        ][:frame_count]

    if frame_count != truth_frame_count:
        fail(
            "E_FRAME_MISMATCH",
            "feature frame count does not match per-frame truth",
            dialog_id=dialog_id,
            actual_frame_count=frame_count,
            expected_frame_count=truth_frame_count,
            frame_samples=frame_samples,
            hop_samples=hop_samples,
        )

    estimated_bytes = frame_count * len(FEATURE_NAMES) * 8
    if estimated_bytes > config.limits.max_resident_feature_bytes:
        fail(
            "E_RESOURCE_LIMIT",
            "resident feature matrix exceeds configured limit",
            dialog_id=dialog_id,
            estimated_bytes=estimated_bytes,
            limit_bytes=config.limits.max_resident_feature_bytes,
        )

    features = _compute_features(frames, config.sample_rate, frame_samples, config)
    matrix = [
        [quantize(value) for value in row]
        for row in features
    ]
    product = {
        "dialog_id": dialog_id,
        "sample_rate": config.sample_rate,
        "frame_ms": config.frame_ms,
        "frame_shift_ms": config.frame_shift_ms,
        "frame_samples": frame_samples,
        "hop_samples": hop_samples,
        "frame_count": frame_count,
        "feature_names": list(FEATURE_NAMES),
        "features": matrix,
        "feature_matrix_hash": content_hash(
            {"feature_names": list(FEATURE_NAMES), "features": matrix}
        ),
    }
    return product


def raw_feature_matrix(
    pcm: np.ndarray,
    config: PipelineConfig,
) -> np.ndarray:
    """Return raw features without a truth-count contract.

    This is used for registration samples, which have no per-frame truth file.
    """
    config.validate()
    signal = np.asarray(pcm, dtype=np.float64) / 32768.0
    frame_samples = config.frame_samples()
    hop_samples = config.frame_shift_samples()
    total_samples = len(signal)
    if total_samples < frame_samples:
        return np.empty((0, len(FEATURE_NAMES)), dtype=np.float64)
    frame_count = 1 + (total_samples - frame_samples) // hop_samples
    frames = np.lib.stride_tricks.sliding_window_view(signal, frame_samples)[
        ::hop_samples
    ][:frame_count]
    return _compute_features(frames, config.sample_rate, frame_samples, config)


def _compute_features(
    frames: np.ndarray,
    sample_rate: int,
    frame_samples: int,
    config: PipelineConfig,
) -> np.ndarray:
    if len(frames) == 0:
        return np.empty((0, len(FEATURE_NAMES)), dtype=np.float64)
    energy = np.mean(frames * frames, axis=1)
    if frame_samples > 1:
        signs = np.signbit(frames)
        zcr = np.mean(signs[:, 1:] != signs[:, :-1], axis=1)
    else:
        zcr = np.zeros(len(frames), dtype=np.float64)
    window = np.hanning(frame_samples)
    spectrum = np.abs(np.fft.rfft(frames * window, axis=1))
    frequencies = np.fft.rfftfreq(frame_samples, d=1.0 / sample_rate)
    magnitude_sum = np.sum(spectrum, axis=1)
    safe_sum = np.where(magnitude_sum > 1e-20, magnitude_sum, 1.0)
    centroid = np.sum(spectrum * frequencies, axis=1) / safe_sum
    centroid = np.where(magnitude_sum > 1e-20, centroid, 0.0)
    cumulative = np.cumsum(spectrum, axis=1)
    threshold = config.rolloff_percent * cumulative[:, -1]
    rolloff_bins = np.argmax(cumulative >= threshold[:, None], axis=1)
    rolloff = frequencies[rolloff_bins]
    rolloff = np.where(cumulative[:, -1] > 1e-20, rolloff, 0.0)
    return np.column_stack([energy, zcr, centroid, rolloff])
