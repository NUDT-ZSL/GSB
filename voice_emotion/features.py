"""Stage 1: deterministic short-time acoustic features."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .canonical import decode_array, encode_array, hash_value
from .config import FeatureConfig
from .errors import E_FRAME_CONFIG, raise_error


FEATURE_NAMES = ("rms_energy", "zero_crossing_rate", "spectral_centroid_hz", "spectral_rolloff_hz")


@dataclass(frozen=True)
class FeatureResult:
    features: np.ndarray
    frame_starts: np.ndarray
    frame_indices: np.ndarray
    feature_names: tuple[str, ...]
    dtype: str

    @property
    def frame_count(self) -> int:
        return int(self.features.shape[0])

    @property
    def output_hash(self) -> str:
        return hash_value(
            {
                "kind": "feature-output",
                "features": encode_array(self.features),
                "frame_starts": encode_array(self.frame_starts),
                "frame_indices": encode_array(self.frame_indices),
                "feature_names": list(self.feature_names),
                "dtype": self.dtype,
            }
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "features": encode_array(self.features),
            "frame_starts": encode_array(self.frame_starts),
            "frame_indices": encode_array(self.frame_indices),
            "feature_names": list(self.feature_names),
            "dtype": self.dtype,
            "frame_count": self.frame_count,
            "output_hash": self.output_hash,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "FeatureResult":
        result = cls(
            features=decode_array(payload["features"]),
            frame_starts=decode_array(payload["frame_starts"]),
            frame_indices=decode_array(payload["frame_indices"]),
            feature_names=tuple(payload["feature_names"]),
            dtype=str(payload["dtype"]),
        )
        if result.output_hash != payload.get("output_hash"):
            from .errors import E_CACHE_CORRUPT

            raise_error(E_CACHE_CORRUPT, "feature payload output hash mismatch")
        return result


def frame_count_for(sample_count: int, config: FeatureConfig, sample_rate: int) -> int:
    frame_samples = config.frame_samples(sample_rate)
    hop_samples = config.hop_samples(sample_rate)
    if frame_samples <= 0 or hop_samples <= 0:
        raise_error(E_FRAME_CONFIG, "frame and hop must be positive")
    if sample_count < frame_samples:
        return 0
    return 1 + (int(sample_count) - frame_samples) // hop_samples


def _window(length: int, mode: str) -> np.ndarray:
    if mode == "rectangular":
        return np.ones(length, dtype=np.float64)
    indices = np.arange(length, dtype=np.float64)
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * indices / float(length))


def extract_features(
    samples: np.ndarray,
    config: FeatureConfig,
    sample_rate: int = 16_000,
    output_dtype: str | None = None,
) -> FeatureResult:
    config.validate(sample_rate)
    dtype = output_dtype or config.dtype
    if dtype not in {"float64", "float32"}:
        raise_error(E_FRAME_CONFIG, "unsupported feature output dtype", dtype=dtype)
    pcm = np.asarray(samples, dtype=np.float64)
    if pcm.ndim != 1:
        raise_error(E_FRAME_CONFIG, "samples must be one-dimensional")
    frame_samples = config.frame_samples(sample_rate)
    hop_samples = config.hop_samples(sample_rate)
    if sample_count := int(pcm.shape[0]):
        starts = np.arange(
            0,
            sample_count - frame_samples + 1,
            hop_samples,
            dtype=np.int64,
        )
    else:
        starts = np.zeros(0, dtype=np.int64)
    count = int(starts.shape[0])
    features = np.zeros((count, len(FEATURE_NAMES)), dtype=np.float64)
    if count:
        window = _window(frame_samples, config.window)
        frequencies = np.fft.rfftfreq(int(config.fft_size), d=1.0 / float(sample_rate))
        for index, start in enumerate(starts.tolist()):
            frame = pcm[start : start + frame_samples]
            energy = float(np.dot(frame, frame) / float(frame_samples))
            signs = np.signbit(frame)
            crossings = int(np.count_nonzero(signs[1:] != signs[:-1]))
            zcr = float(crossings) / float(max(1, frame_samples - 1))
            windowed = frame * window
            magnitude = np.abs(np.fft.rfft(windowed, n=int(config.fft_size)))
            total = float(np.sum(magnitude))
            if total > 0.0:
                centroid = float(np.sum(frequencies * magnitude) / total)
                cumulative = np.cumsum(magnitude)
                threshold = float(config.rolloff_percent) * total
                rolloff_index = int(np.searchsorted(cumulative, threshold, side="left"))
                rolloff_index = min(rolloff_index, frequencies.shape[0] - 1)
                rolloff = float(frequencies[rolloff_index])
            else:
                centroid = 0.0
                rolloff = 0.0
            features[index, 0] = energy
            features[index, 1] = zcr
            features[index, 2] = centroid
            features[index, 3] = rolloff
    if dtype == "float32":
        features = features.astype(np.float32)
    return FeatureResult(
        features=np.ascontiguousarray(features),
        frame_starts=np.ascontiguousarray(starts, dtype=np.int64),
        frame_indices=np.arange(count, dtype=np.int64),
        feature_names=FEATURE_NAMES,
        dtype=dtype,
    )
