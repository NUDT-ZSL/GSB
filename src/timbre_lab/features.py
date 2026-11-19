"""Deterministic frame-level acoustic features."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .config import FrameConfig
from .deterministic import canonical_json_bytes, canonical_json_text, sha256_bytes
from .errors import DomainError, E_FRAME_MISMATCH
from .synthesis import TruthFrame, read_truth


SPECTRAL_BANDS_HZ: tuple[tuple[float, float], ...] = (
    (0.0, 500.0),
    (500.0, 1000.0),
    (1000.0, 1500.0),
    (1500.0, 2200.0),
    (2200.0, 3000.0),
    (3000.0, 4000.0),
    (4000.0, 8000.0),
)


@dataclass(frozen=True)
class FeatureTable:
    utterance_id: str
    sample_rate: int
    frame_ms: float
    hop_ms: float
    frame_length: int
    hop_length: int
    frame_indices: np.ndarray
    start_samples: np.ndarray
    end_samples: np.ndarray
    energy: np.ndarray
    zcr: np.ndarray
    f0_candidate: np.ndarray
    autocorr_peak: np.ndarray
    rms: np.ndarray
    spectral_centroid_hz: np.ndarray
    spectral_bandwidth_hz: np.ndarray
    spectral_flatness: np.ndarray
    band_log_energy_ratio: np.ndarray

    @property
    def frame_count(self) -> int:
        return int(self.frame_indices.shape[0])

    def rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index in range(self.frame_count):
            rows.append(
                {
                    "frame_index": int(self.frame_indices[index]),
                    "start_sample": int(self.start_samples[index]),
                    "end_sample": int(self.end_samples[index]),
                    "energy": float(self.energy[index]),
                    "zcr": float(self.zcr[index]),
                    "f0_candidate": float(self.f0_candidate[index]),
                    "autocorr_peak": float(self.autocorr_peak[index]),
                    "rms": float(self.rms[index]),
                    "spectral_centroid_hz": float(self.spectral_centroid_hz[index]),
                    "spectral_bandwidth_hz": float(self.spectral_bandwidth_hz[index]),
                    "spectral_flatness": float(self.spectral_flatness[index]),
                    "band_log_energy_ratio": [
                        float(value) for value in self.band_log_energy_ratio[index]
                    ],
                }
            )
        return rows

    def to_jsonl_bytes(self) -> bytes:
        return "".join(canonical_json_text(row) + "\n" for row in self.rows()).encode("utf-8")

    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.rows()))


def _frame_starts(
    sample_count: int,
    frame_length: int,
    hop_length: int,
    utterance_id: str,
) -> np.ndarray:
    if sample_count < frame_length:
        raise DomainError(
            E_FRAME_MISMATCH,
            "PCM is shorter than one analysis frame",
            stage="feature_frames",
            utterance_id=utterance_id,
            details={
                "first_mismatch_frame": 0,
                "sample_count": sample_count,
                "frame_samples": frame_length,
            },
        )
    frame_count = 1 + (sample_count - frame_length) // hop_length
    return np.arange(frame_count, dtype=np.int64) * hop_length


def _f0_candidates(
    frames: np.ndarray,
    sample_rate: int,
    frame_length: int,
) -> tuple[np.ndarray, np.ndarray]:
    frame_count = frames.shape[0]
    f0_values = np.zeros(frame_count, dtype=np.float64)
    peaks = np.zeros(frame_count, dtype=np.float64)
    min_lag = max(2, int(math.ceil(sample_rate / 400.0)))
    max_lag = min(frame_length - 2, int(math.floor(sample_rate / 70.0)))
    if max_lag <= min_lag:
        return f0_values, peaks
    for frame_index in range(frame_count):
        frame = frames[frame_index] - float(np.mean(frames[frame_index]))
        total = float(np.dot(frame, frame))
        if total <= 1e-20:
            continue
        correlations = np.zeros(max_lag - min_lag + 3, dtype=np.float64)
        for lag in range(min_lag - 1, max_lag + 2):
            left = frame[: frame_length - lag]
            right = frame[lag:]
            denominator = math.sqrt(float(np.dot(left, left)) * float(np.dot(right, right)))
            if denominator > 1e-20:
                correlations[lag - (min_lag - 1)] = float(np.dot(left, right)) / denominator
        search = correlations[1:-1]
        global_index = int(np.argmax(search))
        global_peak = float(search[global_index])
        if global_peak < 0.16:
            continue
        local_peaks = [
            index
            for index in range(1, len(search) - 1)
            if search[index] >= search[index - 1]
            and search[index] >= search[index + 1]
            and search[index] >= global_peak - 0.03
        ]
        if not local_peaks:
            local_peaks = [global_index]
        # A harmonic waveform can correlate slightly more strongly at 2T than
        # at T. The smallest near-maximum local peak is the fundamental period.
        relative_index = min(local_peaks)
        peak = float(search[relative_index])
        lag = min_lag + relative_index
        refined_lag = float(lag)
        previous = float(correlations[relative_index])
        current = peak
        following = float(correlations[relative_index + 2])
        curvature = previous - 2.0 * current + following
        if abs(curvature) > 1e-15:
            offset = 0.5 * (previous - following) / curvature
            if -0.5 <= offset <= 0.5:
                refined_lag = lag + offset
        f0_values[frame_index] = sample_rate / refined_lag
        peaks[frame_index] = peak
    return f0_values, peaks


def extract_features(
    pcm: np.ndarray,
    *,
    utterance_id: str,
    config: FrameConfig | None = None,
) -> FeatureTable:
    """Extract features using exact integer sample boundaries."""

    frame_config = config or FrameConfig()
    frame_config.validate()
    samples = np.asarray(pcm)
    if samples.ndim != 1:
        raise ValueError("PCM must be one-dimensional")
    if np.issubdtype(samples.dtype, np.integer):
        signal = samples.astype(np.float64) / 32768.0
    else:
        signal = samples.astype(np.float64, copy=False)
    frame_length = frame_config.frame_samples
    hop_length = frame_config.hop_samples
    starts = _frame_starts(signal.shape[0], frame_length, hop_length, utterance_id)
    ends = starts + frame_length
    frame_count = int(starts.shape[0])
    frames = np.stack([signal[start:end] for start, end in zip(starts, ends)], axis=0)
    energy = np.mean(frames * frames, axis=1)
    rms = np.sqrt(energy)
    signs = frames >= 0.0
    zcr = np.mean(signs[:, 1:] != signs[:, :-1], axis=1)
    f0_candidate, autocorr_peak = _f0_candidates(
        frames,
        frame_config.sample_rate,
        frame_length,
    )
    window = np.hanning(frame_length).astype(np.float64)
    spectrum = np.fft.rfft(frames * window, axis=1)
    power = np.abs(spectrum) ** 2
    frequencies = np.fft.rfftfreq(frame_length, d=1.0 / frame_config.sample_rate)
    power_sum = np.sum(power, axis=1)
    safe_sum = np.maximum(power_sum, 1e-30)
    centroid = np.sum(power * frequencies[None, :], axis=1) / safe_sum
    bandwidth = np.sqrt(
        np.sum(power * (frequencies[None, :] - centroid[:, None]) ** 2, axis=1) / safe_sum
    )
    flatness = np.exp(np.mean(np.log(power + 1e-30), axis=1)) / (
        np.mean(power, axis=1) + 1e-30
    )
    band_log_ratios = np.empty((frame_count, len(SPECTRAL_BANDS_HZ)), dtype=np.float64)
    for band_index, (low_hz, high_hz) in enumerate(SPECTRAL_BANDS_HZ):
        mask = (frequencies >= low_hz) & (frequencies < high_hz)
        if band_index == len(SPECTRAL_BANDS_HZ) - 1:
            mask = frequencies >= low_hz
        band_power = np.sum(power[:, mask], axis=1)
        band_log_ratios[:, band_index] = np.log(band_power / safe_sum + 1e-12)
    return FeatureTable(
        utterance_id=utterance_id,
        sample_rate=frame_config.sample_rate,
        frame_ms=frame_config.frame_ms,
        hop_ms=frame_config.hop_ms,
        frame_length=frame_length,
        hop_length=hop_length,
        frame_indices=np.arange(frame_count, dtype=np.int64),
        start_samples=starts.astype(np.int64),
        end_samples=ends.astype(np.int64),
        energy=np.asarray(energy, dtype=np.float64),
        zcr=np.asarray(zcr, dtype=np.float64),
        f0_candidate=np.asarray(f0_candidate, dtype=np.float64),
        autocorr_peak=np.asarray(autocorr_peak, dtype=np.float64),
        rms=np.asarray(rms, dtype=np.float64),
        spectral_centroid_hz=np.asarray(centroid, dtype=np.float64),
        spectral_bandwidth_hz=np.asarray(bandwidth, dtype=np.float64),
        spectral_flatness=np.asarray(flatness, dtype=np.float64),
        band_log_energy_ratio=band_log_ratios,
    )


def validate_frame_alignment(
    features: FeatureTable,
    truth: Iterable[TruthFrame],
    *,
    utterance_id: str,
) -> None:
    truth_frames = tuple(truth)
    feature_indices = [int(value) for value in features.frame_indices]
    truth_indices = [int(frame.frame_index) for frame in truth_frames]
    mismatch: int | None = None
    if feature_indices != truth_indices:
        limit = min(len(feature_indices), len(truth_indices))
        mismatch = limit
        for index in range(limit):
            if feature_indices[index] != truth_indices[index]:
                mismatch = index
                break
    if mismatch is not None:
        feature_value = (
            feature_indices[mismatch] if mismatch < len(feature_indices) else None
        )
        truth_value = truth_indices[mismatch] if mismatch < len(truth_indices) else None
        raise DomainError(
            E_FRAME_MISMATCH,
            "feature frame count/index sequence does not match the truth file",
            stage="feature_frames",
            utterance_id=utterance_id,
            details={
                "first_mismatch_frame": mismatch,
                "feature_frame_count": len(feature_indices),
                "truth_frame_count": len(truth_indices),
                "feature_frame_index": feature_value,
                "truth_frame_index": truth_value,
            },
        )


def extract_and_validate(
    pcm: np.ndarray,
    truth: Iterable[TruthFrame],
    *,
    utterance_id: str,
    config: FrameConfig | None = None,
) -> FeatureTable:
    features = extract_features(pcm, utterance_id=utterance_id, config=config)
    validate_frame_alignment(features, truth, utterance_id=utterance_id)
    return features


def load_truth_file(path: Path) -> tuple[TruthFrame, ...]:
    return read_truth(path)


def f0_energy_observation(
    features: FeatureTable,
    truth: Iterable[TruthFrame],
) -> dict[str, Any]:
    truth_frames = tuple(truth)
    if features.frame_count != len(truth_frames):
        validate_frame_alignment(features, truth_frames, utterance_id=features.utterance_id)
    f0_error = np.abs(features.f0_candidate - np.asarray([frame.f0_true for frame in truth_frames]))
    energy_error = np.abs(
        features.energy - np.asarray([frame.energy_true for frame in truth_frames])
    )
    f0_true = np.asarray([frame.f0_true for frame in truth_frames], dtype=np.float64)
    energy_true = np.asarray([frame.energy_true for frame in truth_frames], dtype=np.float64)
    f0_relative = f0_error / np.maximum(f0_true, 1e-12)
    energy_relative = energy_error / np.maximum(energy_true, 1e-12)
    return {
        "reference_tier": "float64",
        "observations": {
            "frame_index": [int(value) for value in features.frame_indices],
            "f0_true_hz": [float(value) for value in f0_true],
            "f0_observed_hz": [float(value) for value in features.f0_candidate],
            "f0_abs_error_hz": [float(value) for value in f0_error],
            "energy_true": [float(value) for value in energy_true],
            "energy_observed": [float(value) for value in features.energy],
            "energy_abs_error": [float(value) for value in energy_error],
        },
        "summary": {
            "f0_mae_hz": float(np.mean(f0_error)),
            "f0_relative_mae": float(np.mean(f0_relative)),
            "energy_mae": float(np.mean(energy_error)),
            "energy_relative_mae": float(np.mean(energy_relative)),
            "combined_relative_mae": float(np.mean(np.concatenate([f0_relative, energy_relative]))),
        },
        "definition": {
            "f0": "frame-local autocorrelation candidate in Hz compared with f0_true",
            "energy": "unwindowed mean square PCM amplitude compared with energy_true",
            "frame_boundaries": "integer sample indices: start=i*hop_samples, end=start+frame_samples",
        },
    }
