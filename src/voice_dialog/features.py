"""Frame-wise acoustic features with fixed operations and quantized F0."""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np

from .audio import AudioScan, decode_audio, validate_samples
from .constants import (
    E_NONFINITE_FEATURE,
    F0_MAX_HZ,
    F0_MIN_HZ,
    F0_QUANTUM_HZ,
    FEATURE_NAMES,
)
from .errors import BusinessError
from .util import canonicalize, stable_float


def _quantize_f0(value: float) -> int:
    if not math.isfinite(value) or value < F0_MIN_HZ or value > F0_MAX_HZ:
        return 0
    return int(math.floor(value / F0_QUANTUM_HZ + 0.5)) * F0_QUANTUM_HZ


def _estimate_f0(frame: np.ndarray, sample_rate: int) -> tuple[float, int]:
    energy = float(np.sqrt(np.mean(np.square(frame)) + 1e-12))
    if energy < 1e-5:
        return 0.0, 0
    view = frame - float(np.mean(frame))
    view = view * np.hanning(view.size)
    nfft = 1 << (2 * view.size - 1).bit_length()
    spectrum = np.fft.rfft(view, n=nfft)
    autocorr = np.fft.irfft(spectrum * np.conjugate(spectrum), n=nfft)[: view.size]
    zero_lag = float(autocorr[0])
    if not math.isfinite(zero_lag) or zero_lag <= 1e-12:
        return 0.0, 0
    min_lag = max(1, int(math.floor(sample_rate / F0_MAX_HZ)))
    max_lag = min(view.size - 1, int(math.ceil(sample_rate / F0_MIN_HZ)))
    if max_lag <= min_lag:
        return 0.0, 0
    normalized = autocorr[min_lag : max_lag + 1] / zero_lag
    best_offset = int(np.argmax(normalized))
    best_value = float(normalized[best_offset])
    if best_value < 0.25:
        return 0.0, 0
    lag = float(min_lag + best_offset)
    # Deterministic parabolic interpolation, followed by fixed 10-Hz quantization.
    if 0 < best_offset < normalized.size - 1:
        left = float(normalized[best_offset - 1])
        right = float(normalized[best_offset + 1])
        denom = left - 2.0 * best_value + right
        if abs(denom) > 1e-12:
            lag += 0.5 * (left - right) / denom
    frequency = sample_rate / max(lag, 1e-9)
    return frequency, _quantize_f0(frequency)


def _frame_stream(samples: np.ndarray, frame_length: int, frame_shift: int) -> np.ndarray:
    if samples.size == 0:
        return np.empty((0, frame_length), dtype=np.float64)
    frame_count = int(math.ceil(samples.size / frame_shift))
    out = np.zeros((frame_count, frame_length), dtype=np.float64)
    for index in range(frame_count):
        start = index * frame_shift
        stop = min(start + frame_length, samples.size)
        if start < samples.size:
            out[index, : stop - start] = samples[start:stop]
    return out


def _percentile(values: np.ndarray, q: float) -> float:
    if values.size == 0:
        return 0.0
    return float(np.percentile(values, q))


def _segment_features(
    frame_records: list[dict],
    frame_energies: np.ndarray,
    frame_shift: int,
    sample_rate: int,
    duration_sec: float,
) -> dict[str, float]:
    count = len(frame_records)
    f0_values = np.asarray([record["f0_hz"] for record in frame_records], dtype=np.float64)
    energy_values = frame_energies.astype(np.float64, copy=False)
    zcr_values = np.asarray([record["zcr"] for record in frame_records], dtype=np.float64)
    voiced = f0_values > 0
    positive_f0 = f0_values[voiced]

    if positive_f0.size:
        f0_mean = float(np.mean(positive_f0))
        f0_std = float(np.std(positive_f0))
        f0_range = _percentile(positive_f0, 95) - _percentile(positive_f0, 5)
        diffs = np.abs(np.diff(positive_f0))
        f0_slope = float(np.mean(diffs) / (frame_shift / sample_rate)) if diffs.size else 0.0
    else:
        f0_mean = f0_std = f0_range = f0_slope = 0.0

    energy_mean = float(np.mean(energy_values)) if count else 0.0
    energy_std = float(np.std(energy_values)) if count else 0.0
    energy_dynamic_range = _percentile(energy_values, 95) - _percentile(energy_values, 5)
    zcr_mean = float(np.mean(zcr_values)) if count else 0.0
    zcr_std = float(np.std(zcr_values)) if count else 0.0
    voiced_ratio = float(np.mean(voiced)) if count else 0.0

    energy_q90 = _percentile(energy_values, 90)
    activity_threshold = max(0.01, 0.30 * energy_q90)
    active = energy_values >= activity_threshold
    if active.size > 1:
        rising = active & np.logical_not(np.concatenate(([False], active[:-1])))
    else:
        rising = active.copy()
    onset_count = int(np.count_nonzero(rising))
    safe_duration = max(duration_sec, frame_shift / sample_rate)
    speech_rate = onset_count / safe_duration
    onset_indices = np.flatnonzero(rising)
    if onset_indices.size >= 2:
        intervals = np.diff(onset_indices).astype(np.float64) * frame_shift / sample_rate
        interval_mean = float(np.mean(intervals))
        cv = float(np.std(intervals) / interval_mean) if interval_mean > 1e-12 else 1.0
        rhythm_regularity = 1.0 / (1.0 + cv)
    else:
        rhythm_regularity = 0.5
    pause_ratio = float(1.0 - np.mean(active)) if count else 1.0
    activity_ratio = float(np.mean(active)) if count else 0.0

    values = {
        "f0_mean": f0_mean,
        "f0_std": f0_std,
        "f0_range": f0_range,
        "f0_slope": f0_slope,
        "energy_mean": energy_mean,
        "energy_std": energy_std,
        "energy_dynamic_range": energy_dynamic_range,
        "zcr_mean": zcr_mean,
        "zcr_std": zcr_std,
        "voiced_ratio": voiced_ratio,
        "speech_rate": speech_rate,
        "rhythm_regularity": rhythm_regularity,
        "pause_ratio": pause_ratio,
        "activity_ratio": activity_ratio,
    }
    return {name: stable_float(values[name], 6) for name in FEATURE_NAMES}


def _instant_rhythm(frame_energies: np.ndarray, frame_shift: int, sample_rate: int) -> tuple[list[float], list[float]]:
    if frame_energies.size == 0:
        return [], []
    energy_q90 = _percentile(frame_energies, 90)
    threshold = max(0.01, 0.30 * energy_q90)
    active = frame_energies >= threshold
    radius = 8
    rates: list[float] = []
    regularities: list[float] = []
    hop_sec = frame_shift / sample_rate
    for index in range(frame_energies.size):
        start = max(0, index - radius)
        stop = min(frame_energies.size, index + radius + 1)
        local_active = active[start:stop]
        local_energy = frame_energies[start:stop]
        if local_active.size > 1:
            local_rising = local_active & np.logical_not(
                np.concatenate(([False], local_active[:-1]))
            )
        else:
            local_rising = local_active
        local_duration = max(1, local_active.size) * hop_sec
        rates.append(stable_float(float(np.count_nonzero(local_rising)) / local_duration))
        mean_energy = float(np.mean(local_energy))
        if local_energy.size > 1 and mean_energy > 1e-12:
            roughness = float(np.mean(np.abs(np.diff(local_energy))) / mean_energy)
            regularities.append(stable_float(1.0 / (1.0 + roughness)))
        else:
            regularities.append(0.0)
    return rates, regularities


def extract_audio_features(
    scan: AudioScan,
    *,
    frame_length_ms: int,
    frame_shift_ms: int,
    effective_features: Iterable[str],
    injections: Iterable[dict] | None = None,
    injection_id: str = "",
) -> dict:
    injections_list = list(injections or [])
    effective = list(effective_features)
    unknown = [name for name in effective if name not in FEATURE_NAMES]
    if unknown:
        raise ValueError(f"unknown feature names: {unknown}")
    for item in injections_list:
        if item.get("kind") == "nonfinite_sample" and item.get("target") == scan.filename:
            injection_id = str(item.get("id", injection_id))
    decoded = decode_audio(scan, injections_list)
    validate_samples(decoded.samples, filename=scan.filename, injection_id=injection_id)
    sample_rate = decoded.header.sample_rate
    frame_length = int(round(frame_length_ms * sample_rate / 1000.0))
    frame_shift = int(round(frame_shift_ms * sample_rate / 1000.0))
    if frame_length < 2 or frame_shift < 1:
        raise ValueError("frame length and shift must yield at least one sample")
    frames = _frame_stream(decoded.samples, frame_length, frame_shift)
    frame_records: list[dict] = []
    frame_energies = np.zeros(frames.shape[0], dtype=np.float64)
    for index, frame in enumerate(frames):
        energy = float(np.sqrt(np.mean(np.square(frame)) + 1e-12))
        if frame.size > 1:
            zcr = float(np.count_nonzero(frame[1:] * frame[:-1] < 0.0) / (frame.size - 1))
        else:
            zcr = 0.0
        frequency, quantized_f0 = _estimate_f0(frame, sample_rate)
        frame_energies[index] = energy
        frame_records.append(
            {
                "index": index,
                "time_sec": stable_float(index * frame_shift / sample_rate),
                "f0_hz": quantized_f0,
                "f0_estimate_hz": stable_float(frequency),
                "energy": stable_float(energy),
                "zcr": stable_float(zcr),
                "voiced": int(quantized_f0 > 0),
            }
        )
    rates, regularities = _instant_rhythm(frame_energies, frame_shift, sample_rate)
    for index, record in enumerate(frame_records):
        record["speech_rate_inst"] = rates[index]
        record["rhythm_inst"] = regularities[index]
    segment_features = _segment_features(
        frame_records,
        frame_energies,
        frame_shift,
        sample_rate,
        decoded.header.duration_sec,
    )
    vector = [segment_features[name] for name in effective]
    if not all(math.isfinite(value) for value in vector):
        raise BusinessError(
            E_NONFINITE_FEATURE,
            "features",
            "aggregated feature vector contains a non-finite value",
            filename=scan.filename,
            exception_type="NonFiniteFeatureError",
            injection_id=injection_id,
        )
    result = {
        "filename": scan.filename,
        "content_sha256": scan.content_sha256,
        "sample_rate": sample_rate,
        "channels": decoded.header.channels,
        "sample_width": decoded.header.sample_width,
        "duration_sec": stable_float(decoded.header.duration_sec),
        "frame_length_samples": frame_length,
        "frame_shift_samples": frame_shift,
        "frame_count": len(frame_records),
        "effective_features": effective,
        "segment_feature_vector": vector,
        "all_segment_features": segment_features,
        "frames": canonicalize(frame_records),
        "nonfinite_injection_id": injection_id if decoded.nonfinite_injected else "",
    }
    return canonicalize(result)
