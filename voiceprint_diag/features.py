"""Deterministic frame-wise acoustic feature extraction."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .audio import AudioData
from .canonical import derived_key, quantize_float_q, round_significant, sha256_obj
from .config import Config


@dataclass(frozen=True)
class FeatureSet:
    sample: str
    audio_sha256: str
    frame_length: int
    hop_length: int
    frames: tuple[dict[str, Any], ...]
    fingerprint: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "sample": self.sample,
            "audio_sha256": self.audio_sha256,
            "frame_length": self.frame_length,
            "hop_length": self.hop_length,
            "frames": list(self.frames),
            "feature_fingerprint": self.fingerprint,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "FeatureSet":
        return cls(
            sample=payload["sample"],
            audio_sha256=payload["audio_sha256"],
            frame_length=int(payload["frame_length"]),
            hop_length=int(payload["hop_length"]),
            frames=tuple(payload["frames"]),
            fingerprint=payload["feature_fingerprint"],
        )


def feature_cache_key(audio_sha256: str, config: Config) -> str:
    return derived_key(
        "feature/v3",
        audio_sha256,
        config.feature_hash(),
        {"fractional_bits": config.fractional_bits, "effective_digits": config.feature_significant_digits},
    )


def _hz_to_mel(hz: np.ndarray | float) -> np.ndarray | float:
    return 2595.0 * np.log10(1.0 + np.asarray(hz, dtype=np.float64) / 700.0)


def _mel_to_hz(mel: np.ndarray | float) -> np.ndarray | float:
    return 700.0 * (np.power(10.0, np.asarray(mel, dtype=np.float64) / 2595.0) - 1.0)


def _mel_filterbank(config: Config, fft_bins: int) -> np.ndarray:
    nyquist = config.expected_sample_rate / 2.0
    low_mel = float(_hz_to_mel(0.0))
    high_mel = float(_hz_to_mel(config.mel_max_freq))
    points = np.linspace(low_mel, high_mel, config.mel_bands + 2, dtype=np.float64)
    hz = np.asarray(_mel_to_hz(points), dtype=np.float64)
    frequencies = np.arange(fft_bins, dtype=np.float64) * nyquist / (fft_bins - 1)
    filters = np.zeros((config.mel_bands, fft_bins), dtype=np.float64)
    for band in range(config.mel_bands):
        left, centre, right = hz[band], hz[band + 1], hz[band + 2]
        if centre <= left or right <= centre:
            continue
        rising = (frequencies - left) / (centre - left)
        falling = (right - frequencies) / (right - centre)
        filters[band] = np.maximum(0.0, np.minimum(rising, falling))
    return filters


def _dct_matrix(bands: int, order: int) -> np.ndarray:
    matrix = np.zeros((order, bands), dtype=np.float64)
    for coefficient in range(1, order + 1):
        scale = math.sqrt(2.0 / bands)
        for band in range(bands):
            matrix[coefficient - 1, band] = scale * math.cos(
                math.pi * coefficient * (2 * band + 1) / (2.0 * bands)
            )
    return matrix


def _estimate_f0(frame: np.ndarray, sample_rate: int, config: Config) -> tuple[float, float]:
    centred = frame - float(np.mean(frame))
    energy = float(np.dot(centred, centred))
    if energy <= 1e-15:
        return 0.0, 0.0
    min_lag = max(1, int(math.floor(sample_rate / config.f0_max_hz)))
    max_lag = min(len(frame) - 2, int(math.ceil(sample_rate / config.f0_min_hz)))
    if max_lag < min_lag:
        return 0.0, 0.0
    autocorrelation = np.correlate(centred, centred, mode="full")
    midpoint = len(centred) - 1
    prefix_energy = np.concatenate(([0.0], np.cumsum(centred * centred)))
    total_energy = float(prefix_energy[-1])
    lags = np.arange(min_lag, max_lag + 1, dtype=np.int64)
    left_energy = prefix_energy[len(centred) - lags]
    right_energy = total_energy - prefix_energy[lags]
    denominator = np.sqrt(left_energy * right_energy)
    correlation_values = np.full(lags.shape, -math.inf, dtype=np.float64)
    valid = denominator > 1e-15
    correlation_values[valid] = autocorrelation[midpoint + lags[valid]] / denominator[valid]
    correlations = [float(item) for item in correlation_values]
    best_index = int(np.argmax(correlation_values))
    best_lag = int(lags[best_index])
    best_correlation = float(correlation_values[best_index])
    if not math.isfinite(best_correlation) or best_correlation < config.f0_voicing_threshold:
        return 0.0, 0.0
    # Period doubling is common when a smoothed envelope makes a multi-period lag
    # stronger. Select the earliest local peak at >=78% of the global
    # peak; exact ties therefore still resolve to the smaller lag.
    strong_threshold = max(config.f0_voicing_threshold, 0.78 * best_correlation)
    chosen_index = best_lag - min_lag
    for offset, correlation in enumerate(correlations):
        if correlation < strong_threshold:
            continue
        previous = correlations[offset - 1] if offset > 0 else -math.inf
        following = correlations[offset + 1] if offset + 1 < len(correlations) else -math.inf
        if correlation >= previous and correlation >= following:
            chosen_index = offset
            break
    chosen_lag = min_lag + chosen_index
    chosen_correlation = correlations[chosen_index]
    return sample_rate / chosen_lag, chosen_correlation


def extract_features(audio: AudioData, config: Config) -> FeatureSet:
    frame_length = config.frame_length
    hop_length = config.hop_length
    sample_count = int(audio.samples.size)
    if sample_count == 0:
        starts: list[int] = []
    elif sample_count <= frame_length:
        starts = [0]
    else:
        full_intervals, remainder = divmod(sample_count - frame_length, hop_length)
        full_frame_count = full_intervals + 1
        starts = [index * hop_length for index in range(full_frame_count)]
        if remainder:
            starts.append(full_frame_count * hop_length)
    fft_size = 1
    while fft_size < frame_length:
        fft_size <<= 1
    rfft_bins = fft_size // 2 + 1
    window = 0.5 - 0.5 * np.cos(2.0 * math.pi * np.arange(frame_length) / frame_length)
    filters = _mel_filterbank(config, rfft_bins)
    dct = _dct_matrix(config.mel_bands, config.cepstral_order)
    digits = config.feature_significant_digits
    frames: list[dict[str, Any]] = []
    for frame_index, start in enumerate(starts):
        raw = audio.samples[start : start + frame_length]
        unpadded_count = int(raw.size)
        if unpadded_count < frame_length:
            frame = np.zeros(frame_length, dtype=np.float64)
            frame[:unpadded_count] = raw
            tail = True
        else:
            frame = raw.astype(np.float64, copy=False)
            tail = False
        log_energy = math.log(max(float(np.mean(frame * frame)), 1e-12))
        if frame_length > 1:
            crossings = np.count_nonzero(np.signbit(frame[1:]) != np.signbit(frame[:-1]))
            zcr = float(crossings) / float(frame_length - 1)
        else:
            zcr = 0.0
        f0_hz, f0_confidence = _estimate_f0(frame, config.expected_sample_rate, config)
        spectrum = np.fft.rfft(frame * window, n=fft_size)
        power = (spectrum.real * spectrum.real + spectrum.imag * spectrum.imag) / float(fft_size)
        mel_energy = filters @ power
        log_mel = np.log(np.maximum(mel_energy, 1e-12))
        cepstral = dct @ log_mel
        rounded_cepstral = [round_significant(float(item), digits) for item in cepstral]
        frames.append(
            {
                "index": frame_index,
                "start_sample": start,
                "available_samples": unpadded_count,
                "end_sample": start + unpadded_count,
                "tail": tail,
                "log_energy": round_significant(log_energy, digits),
                "zcr": round_significant(zcr, digits),
                "f0_hz": round_significant(f0_hz, digits),
                "f0_confidence": round_significant(f0_confidence, digits),
                "cepstral": rounded_cepstral,
            }
        )
    payload_core = {
        "sample": audio.name,
        "audio_sha256": audio.file_sha256,
        "frame_length": frame_length,
        "hop_length": hop_length,
        "frames": frames,
    }
    return FeatureSet(
        sample=audio.name,
        audio_sha256=audio.file_sha256,
        frame_length=frame_length,
        hop_length=hop_length,
        frames=tuple(frames),
        fingerprint=sha256_obj(payload_core),
    )


def feature_frame_vector(frame: dict[str, Any]) -> tuple[float, ...]:
    return (
        float(frame["log_energy"]),
        float(frame["zcr"]),
        float(frame["f0_hz"]),
        *(float(item) for item in frame["cepstral"]),
    )


def quantize_feature_frame(frame: dict[str, Any], bits: int) -> dict[str, Any]:
    log_energy = quantize_float_q(float(frame["log_energy"]), bits)
    zcr = quantize_float_q(float(frame["zcr"]), bits)
    f0_hz = quantize_float_q(float(frame["f0_hz"]), bits)
    cepstral = [
        quantize_float_q(float(item), bits)
        for item in frame["cepstral"]
    ]
    return {
        "log_energy_q": log_energy,
        "zcr_q": zcr,
        "f0_hz_q": f0_hz,
        "cepstral_q": cepstral,
    }


def quantized_vector(frame: dict[str, Any], bits: int) -> tuple[int, ...]:
    quantized = quantize_feature_frame(frame, bits)
    return (
        int(quantized["log_energy_q"]),
        int(quantized["zcr_q"]),
        int(quantized["f0_hz_q"]),
        *(int(item) for item in quantized["cepstral_q"]),
    )


def feature_summary(feature_set: FeatureSet, digits: int) -> dict[str, Any]:
    frames = feature_set.frames
    if not frames:
        return {
            "frame_count": 0,
            "tail_frame_count": 0,
            "feature_fingerprint": feature_set.fingerprint,
        }
    arrays = {
        "log_energy": np.asarray([item["log_energy"] for item in frames], dtype=np.float64),
        "zcr": np.asarray([item["zcr"] for item in frames], dtype=np.float64),
        "f0_hz": np.asarray([item["f0_hz"] for item in frames], dtype=np.float64),
    }
    statistics = {}
    for key, values in arrays.items():
        statistics[key] = {
            "min": round_significant(float(np.min(values)), digits),
            "max": round_significant(float(np.max(values)), digits),
            "mean": round_significant(float(np.mean(values)), digits),
        }
    cepstral = np.asarray([item["cepstral"] for item in frames], dtype=np.float64)
    return {
        "frame_count": len(frames),
        "tail_frame_count": sum(1 for item in frames if item["tail"]),
        "last_frame_start_sample": frames[-1]["start_sample"],
        "statistics": statistics,
        "cepstral_mean": [round_significant(float(item), digits) for item in np.mean(cepstral, axis=0)],
        "frames": list(frames),
        "first_frames": frames[:2],
        "last_frames": frames[-2:],
        "feature_fingerprint": feature_set.fingerprint,
    }
