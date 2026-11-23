"""Deterministic DSP stages for frame features, timbre, alignment, mapping and synthesis."""

from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np

from .config import PipelineConfig, decimals_for
from .dataset import pcm_hash
from .errors import fail
from .models import ManifestEntry, round_float


FEATURE_VECTOR_DIMENSION = 7


def frame_count_for_samples(sample_count: int, frame_samples: int, hop_samples: int) -> int:
    if sample_count < frame_samples:
        return 0
    return 1 + (sample_count - frame_samples) // hop_samples


def _zero_crossing_rate(frame: np.ndarray) -> float:
    if frame.size < 2:
        return 0.0
    return float(np.count_nonzero(frame[:-1] * frame[1:] < 0.0)) / float(frame.size - 1)


def _f0_candidate(
    frame: np.ndarray, sample_rate: int, minimum_f0: float = 60.0, maximum_f0: float = 400.0
) -> tuple[float, float]:
    centered = frame - float(np.mean(frame))
    if frame.size < 32 or float(np.sum(centered * centered)) <= 1e-12:
        return 0.0, 0.0
    minimum_lag = max(2, int(math.floor(sample_rate / maximum_f0)))
    maximum_lag = min(frame.size - 2, int(math.ceil(sample_rate / minimum_f0)))
    if maximum_lag <= minimum_lag:
        return 0.0, 0.0
    correlations: list[float] = []
    for lag in range(minimum_lag, maximum_lag + 1):
        left = centered[:-lag]
        right = centered[lag:]
        denominator = math.sqrt(float(np.dot(left, left) * np.dot(right, right)))
        if denominator <= 1e-12:
            correlations.append(0.0)
        else:
            correlations.append(float(np.dot(left, right) / denominator))
    best_offset = int(np.argmax(np.asarray(correlations, dtype=np.float64)))
    best_lag = minimum_lag + best_offset
    best_score = correlations[best_offset]
    refined_lag = float(best_lag)
    if 0 < best_offset < len(correlations) - 1:
        previous = correlations[best_offset - 1]
        current = correlations[best_offset]
        following = correlations[best_offset + 1]
        denominator = previous - 2.0 * current + following
        if abs(denominator) > 1e-12:
            delta = 0.5 * (previous - following) / denominator
            refined_lag += max(-0.5, min(0.5, delta))
    if best_score < 0.18:
        return 0.0, max(0.0, best_score)
    return float(sample_rate) / refined_lag, float(best_score)


def _spectral_shape(
    frame: np.ndarray, sample_rate: int
) -> tuple[float, float, float, float]:
    centered = frame - float(np.mean(frame))
    if frame.size < 4 or float(np.sum(centered * centered)) <= 1e-12:
        return 0.0, 0.0, 0.0, 0.0
    window = np.hanning(frame.size)
    spectrum = np.fft.rfft(centered * window)
    power = spectrum.real * spectrum.real + spectrum.imag * spectrum.imag
    frequencies = np.fft.rfftfreq(frame.size, d=1.0 / sample_rate)
    total = float(np.sum(power))
    if total <= 1e-18:
        return 0.0, 0.0, 0.0, 0.0
    centroid = float(np.sum(frequencies * power) / total)
    low = float(np.sum(power[frequencies < 500.0]) / total)
    mid = float(
        np.sum(power[(frequencies >= 500.0) & (frequencies < 2000.0)]) / total
    )
    high = float(np.sum(power[frequencies >= 2000.0]) / total)
    return centroid, low, mid, high


def extract_frame_features(
    pcm: np.ndarray,
    *,
    sample_rate: int,
    frame_samples: int,
    hop_samples: int,
    decimals: int,
) -> tuple[list[dict[str, Any]], int]:
    frame_count = frame_count_for_samples(len(pcm), frame_samples, hop_samples)
    frames: list[dict[str, Any]] = []
    for frame_index in range(frame_count):
        start = frame_index * hop_samples
        end = start + frame_samples
        samples = pcm[start:end].astype(np.float64) / 32768.0
        mean_square = float(np.mean(samples * samples)) if samples.size else 0.0
        rms = math.sqrt(max(0.0, mean_square))
        zcr = _zero_crossing_rate(samples)
        f0, f0_score = _f0_candidate(samples, sample_rate)
        centroid, low, mid, high = _spectral_shape(samples, sample_rate)
        frames.append(
            {
                "frame_index": frame_index,
                "start_sample": start,
                "end_sample": end,
                "energy_sum": round_float(float(np.sum(samples * samples)), decimals),
                "energy_rms": round_float(rms, decimals),
                "zcr": round_float(zcr, decimals),
                "f0_candidate": round_float(f0, decimals),
                "f0_score": round_float(max(0.0, min(1.0, f0_score)), decimals),
                "spectral_centroid_hz": round_float(centroid, decimals),
                "band_low": round_float(low, decimals),
                "band_mid": round_float(mid, decimals),
                "band_high": round_float(high, decimals),
            }
        )
    return frames, frame_count


def extract_features(
    *,
    entry: ManifestEntry,
    pcm: np.ndarray,
    raw_pcm_hash: str,
    config: PipelineConfig,
) -> dict[str, Any]:
    decimals = decimals_for(entry.precision_profile)
    frames, frame_count = extract_frame_features(
        pcm,
        sample_rate=config.sample_rate,
        frame_samples=config.frame_samples,
        hop_samples=config.hop_samples,
        decimals=decimals,
    )
    return {
        "artifact_type": "frame_features",
        "schema_version": 1,
        "utterance_id": entry.utterance_id,
        "speaker_id": entry.speaker_id,
        "ref_id": entry.ref_id,
        "seed": entry.seed,
        "ref_revision": entry.ref_revision,
        "precision_profile": entry.precision_profile,
        "audio_sha256": raw_pcm_hash,
        "sample_rate": config.sample_rate,
        "frame_ms": config.frame_ms,
        "hop_ms": config.hop_ms,
        "frame_samples": config.frame_samples,
        "hop_samples": config.hop_samples,
        "sample_count": int(len(pcm)),
        "frame_count": int(frame_count),
        "extractor_version": config.extractor_version,
        "frames": frames,
    }


def frame_vector(frame: dict[str, Any], sample_rate: int) -> np.ndarray:
    f0 = float(frame.get("f0_candidate", 0.0))
    if f0 <= 1e-9:
        log_f0 = 0.0
    else:
        log_f0 = math.log2(max(60.0, f0) / 100.0)
    vector = np.asarray(
        [
            log_f0,
            math.log(max(1e-8, float(frame.get("energy_rms", 0.0)))),
            4.0 * float(frame.get("zcr", 0.0)),
            float(frame.get("spectral_centroid_hz", 0.0)) / (sample_rate / 2.0),
            float(frame.get("band_low", 0.0)),
            float(frame.get("band_mid", 0.0)),
            float(frame.get("band_high", 0.0)),
        ],
        dtype=np.float64,
    )
    return vector


def _normalize_l2(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        return np.zeros_like(vector)
    return vector / norm


def estimate_timbre(
    *,
    ref_id: str,
    ref_speaker_id: str,
    ref_revision: int,
    ref_pcm: np.ndarray,
    ref_pcm_hash: str,
    config: PipelineConfig,
    precision_profile: str,
) -> dict[str, Any]:
    decimals = decimals_for(precision_profile)
    frames, frame_count = extract_frame_features(
        ref_pcm,
        sample_rate=config.sample_rate,
        frame_samples=config.frame_samples,
        hop_samples=config.hop_samples,
        decimals=decimals,
    )
    if frame_count == 0:
        fail(
            "E_REF_TOO_SHORT",
            "参考音色片段不足以提取一帧",
            field="ref_id",
            details={"ref_id": ref_id, "ref_revision": ref_revision},
        )
    vectors = np.stack([frame_vector(frame, config.sample_rate) for frame in frames])
    positive_f0 = [
        float(frame["f0_candidate"])
        for frame in frames
        if float(frame["f0_candidate"]) > 0.0
    ]
    mean_f0 = float(np.mean(positive_f0)) if positive_f0 else 100.0
    mean_rms = float(np.mean([float(frame["energy_rms"]) for frame in frames]))
    mean_zcr = float(np.mean([float(frame["zcr"]) for frame in frames]))
    mean_centroid = float(
        np.mean([float(frame["spectral_centroid_hz"]) for frame in frames])
    )
    band_low = float(np.mean([float(frame["band_low"]) for frame in frames]))
    band_mid = float(np.mean([float(frame["band_mid"]) for frame in frames]))
    band_high = float(np.mean([float(frame["band_high"]) for frame in frames]))
    raw_vector = np.asarray(
        [
            math.log2(max(60.0, mean_f0) / 100.0),
            math.log(max(1e-8, mean_rms)),
            4.0 * mean_zcr,
            mean_centroid / config.nyquist_hz,
            band_low,
            band_mid,
            band_high,
        ],
        dtype=np.float64,
    )
    normalized = _normalize_l2(raw_vector)

    boundaries = np.linspace(0, frame_count, config.unit_count + 1, dtype=np.int64)
    units: list[dict[str, Any]] = []
    for phoneme_id in range(config.unit_count):
        start = int(boundaries[phoneme_id])
        end = int(boundaries[phoneme_id + 1])
        if end <= start:
            prototype = normalized.copy()
        else:
            prototype = _normalize_l2(np.mean(vectors[start:end], axis=0))
        units.append(
            {
                "speaker_id": ref_speaker_id,
                "phoneme_id": phoneme_id,
                "prototype": [
                    round_float(float(value), decimals) for value in prototype.tolist()
                ],
            }
        )
    stats = {
        "mean_f0_hz": round_float(mean_f0, decimals),
        "mean_rms": round_float(mean_rms, decimals),
        "mean_zcr": round_float(mean_zcr, decimals),
        "mean_spectral_centroid_hz": round_float(mean_centroid, decimals),
        "band_low": round_float(band_low, decimals),
        "band_mid": round_float(band_mid, decimals),
        "band_high": round_float(band_high, decimals),
    }
    return {
        "artifact_type": "timbre_embedding",
        "schema_version": 1,
        "ref_id": ref_id,
        "speaker_id": ref_speaker_id,
        "ref_revision": int(ref_revision),
        "ref_audio_sha256": ref_pcm_hash,
        "precision_profile": precision_profile,
        "sample_rate": config.sample_rate,
        "frame_ms": config.frame_ms,
        "hop_ms": config.hop_ms,
        "frame_samples": config.frame_samples,
        "hop_samples": config.hop_samples,
        "ref_frame_count": int(frame_count),
        "embedding_version": config.embedding_version,
        "normalization": "l2",
        "normalization_scope": "per-speaker-reference",
        "dimension": FEATURE_VECTOR_DIMENSION,
        "raw_vector": [
            round_float(float(value), decimals) for value in raw_vector.tolist()
        ],
        "embedding": [
            round_float(float(value), decimals) for value in normalized.tolist()
        ],
        "stats": stats,
        "units": units,
    }


def choose_alignment_candidate(
    candidates: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Choose the best candidate, with the specified deterministic tie-break."""

    ordered = sorted(
        candidates,
        key=lambda item: (
            -round(float(item["score"]), 12),
            str(item["speaker_id"]),
            int(item["phoneme_id"]),
        ),
    )
    if not ordered:
        fail("E_ALIGNMENT_EMPTY", "没有可用的发音单元候选", field="units")
    return ordered[0]


def align_frames(
    *,
    features: dict[str, Any],
    timbre: dict[str, Any],
    config: PipelineConfig,
) -> dict[str, Any]:
    units = timbre["units"]
    unit_vectors = [
        (str(unit["speaker_id"]), int(unit["phoneme_id"]), np.asarray(unit["prototype"], dtype=np.float64))
        for unit in units
    ]
    aligned: list[dict[str, Any]] = []
    for frame in features["frames"]:
        vector = _normalize_l2(frame_vector(frame, config.sample_rate))
        candidates: list[dict[str, Any]] = []
        for speaker_id, phoneme_id, prototype in unit_vectors:
            score = float(np.dot(vector, prototype))
            candidates.append(
                {
                    "speaker_id": speaker_id,
                    "phoneme_id": phoneme_id,
                    "score": round(score, 12),
                }
            )
        chosen = choose_alignment_candidate(candidates)
        aligned.append(
            {
                "frame_index": int(frame["frame_index"]),
                "speaker_id": str(chosen["speaker_id"]),
                "phoneme_id": int(chosen["phoneme_id"]),
                "score": round(float(chosen["score"]), 12),
            }
        )
    return {
        "artifact_type": "phoneme_alignment",
        "schema_version": 1,
        "utterance_id": features["utterance_id"],
        "frame_count": int(features["frame_count"]),
        "alignment_version": config.alignment_version,
        "tie_break": ["score_desc", "speaker_id_asc", "phoneme_id_asc"],
        "frames": aligned,
    }


def _quantized_numeric(value: float, decimals: int) -> float:
    return round_float(float(value), decimals)


def map_conversion_params(
    *,
    features: dict[str, Any],
    timbre: dict[str, Any],
    alignment: dict[str, Any],
    config: PipelineConfig,
    decimals: int,
) -> dict[str, Any]:
    frames = features["frames"]
    aligned_frames = alignment["frames"]
    if len(frames) != len(aligned_frames):
        fail(
            "E_FRAME_MISMATCH",
            "特征帧数与对齐帧数不一致",
            field="frame_count",
            details={
                "utterance_id": features["utterance_id"],
                "feature_frame_count": len(frames),
                "alignment_frame_count": len(aligned_frames),
                "first_deviation_frame": min(len(frames), len(aligned_frames)),
            },
        )
    positive_f0 = [
        float(frame["f0_candidate"])
        for frame in frames
        if float(frame["f0_candidate"]) > 0.0
    ]
    source_mean_f0 = (
        float(np.mean(positive_f0))
        if positive_f0
        else float(timbre["stats"]["mean_f0_hz"])
    )
    source_mean_rms = float(
        np.mean([float(frame["energy_rms"]) for frame in frames])
    )
    source_mean_centroid = float(
        np.mean([float(frame["spectral_centroid_hz"]) for frame in frames])
    )
    target_mean_f0 = float(timbre["stats"]["mean_f0_hz"])
    target_mean_rms = float(timbre["stats"]["mean_rms"])
    target_centroid = float(timbre["stats"]["mean_spectral_centroid_hz"])
    parameters = config.mapping_params
    f0_strength = float(parameters["f0_strength"])
    energy_strength = float(parameters["energy_strength"])
    formant_strength = float(parameters["formant_strength"])
    min_f0 = float(parameters["min_f0_hz"])
    max_f0 = float(parameters["max_f0_hz"])
    harmonics = int(parameters["harmonics"])
    f0_ratio = target_mean_f0 / max(1e-9, source_mean_f0)
    energy_ratio = target_mean_rms / max(1e-9, source_mean_rms)
    centroid_ratio = target_centroid / max(1e-9, source_mean_centroid)
    formant_scale = min(1.55, max(0.65, centroid_ratio ** formant_strength))

    mapped_frames: list[dict[str, Any]] = []
    phase_cycles = 0.0
    for frame, aligned in zip(frames, aligned_frames):
        source_f0 = float(frame["f0_candidate"])
        if source_f0 <= 0.0:
            source_f0 = source_mean_f0
        mapped_f0 = source_f0 * (f0_ratio ** f0_strength)
        mapped_f0 = min(max_f0, max(min_f0, mapped_f0))
        mapped_energy = float(frame["energy_rms"]) * (
            energy_ratio ** energy_strength
        )
        mapped_energy = max(1e-8, mapped_energy)
        phoneme_id = int(aligned["phoneme_id"])
        weights = np.zeros(harmonics, dtype=np.float64)
        for harmonic in range(1, harmonics + 1):
            frequency = harmonic * mapped_f0
            if frequency < 1000.0:
                band_gain = 0.55 + 0.9 * float(timbre["stats"]["band_low"])
            elif frequency < 2500.0:
                band_gain = 0.55 + 0.9 * float(timbre["stats"]["band_mid"])
            else:
                band_gain = 0.45 + 0.9 * float(timbre["stats"]["band_high"])
            tilt = 1.0 + 0.035 * ((phoneme_id % 4) - 1.5) * ((harmonic % 3) - 1)
            weights[harmonic - 1] = band_gain * tilt / harmonic
        weight_rms = math.sqrt(max(1e-12, float(np.sum(weights * weights)) * 0.5))
        amplitude = mapped_energy / weight_rms
        sample_start = int(frame["start_sample"])
        mapped_frames.append(
            {
                "frame_index": int(frame["frame_index"]),
                "sample_start": sample_start,
                "mapped_f0_hz": _quantized_numeric(mapped_f0, decimals),
                "mapped_energy_rms": _quantized_numeric(mapped_energy, decimals),
                "amplitude": _quantized_numeric(amplitude, decimals),
                "formant_scale": _quantized_numeric(formant_scale, decimals),
                "phase_start_cycles": _quantized_numeric(phase_cycles, decimals),
                "unit_speaker_id": str(aligned["speaker_id"]),
                "phoneme_id": phoneme_id,
                "alignment_score": _quantized_numeric(float(aligned["score"]), decimals),
                "harmonic_weights": [
                    _quantized_numeric(float(value), decimals) for value in weights.tolist()
                ],
            }
        )
        phase_cycles += mapped_f0 * config.frame_samples / config.sample_rate
    return {
        "artifact_type": "conversion_params",
        "schema_version": 1,
        "utterance_id": features["utterance_id"],
        "mapping_version": config.mapping_version,
        "mapping_config_hash": config.mapping_config_hash(),
        "precision_profile": features["precision_profile"],
        "sample_rate": config.sample_rate,
        "frame_ms": config.frame_ms,
        "hop_ms": config.hop_ms,
        "frame_samples": config.frame_samples,
        "hop_samples": config.hop_samples,
        "sample_count": int(features["sample_count"]),
        "frame_count": int(features["frame_count"]),
        "source_mean_f0_hz": _quantized_numeric(source_mean_f0, decimals),
        "source_mean_rms": _quantized_numeric(source_mean_rms, decimals),
        "target_mean_f0_hz": _quantized_numeric(target_mean_f0, decimals),
        "target_mean_rms": _quantized_numeric(target_mean_rms, decimals),
        "frames": mapped_frames,
    }


def synthesize_from_params(
    params: dict[str, Any],
    *,
    frame_samples: int,
    hop_samples: int,
    sample_count: int | None = None,
) -> np.ndarray:
    output_samples = int(params["sample_count"] if sample_count is None else sample_count)
    buffer = np.zeros(output_samples, dtype=np.float64)
    normalization = np.zeros(output_samples, dtype=np.float64)
    overlap = hop_samples != frame_samples
    for frame in params["frames"]:
        start = int(frame["sample_start"])
        if start >= output_samples:
            continue
        available = min(frame_samples, output_samples - start)
        if available <= 0:
            continue
        mapped_f0 = float(frame["mapped_f0_hz"])
        phase_start = float(frame["phase_start_cycles"])
        t = np.arange(available, dtype=np.float64) / float(params["sample_rate"])
        phase = 2.0 * math.pi * (phase_start + mapped_f0 * t)
        signal = np.zeros(available, dtype=np.float64)
        weights = np.asarray(frame["harmonic_weights"], dtype=np.float64)
        for harmonic, weight in enumerate(weights, start=1):
            signal += float(weight) * np.sin(harmonic * phase + 0.17 * harmonic)
        signal *= float(frame["amplitude"])
        if overlap:
            window = np.hanning(frame_samples)[:available]
            window = np.maximum(window, 1e-3)
        else:
            window = np.ones(available, dtype=np.float64)
        buffer[start : start + available] += signal * window
        normalization[start : start + available] += window
    covered = normalization > 1e-9
    buffer[covered] /= normalization[covered]
    return np.rint(np.clip(buffer, -0.97, 0.97) * 32767.0).astype(np.int16)


def synthesize_pcm(params: dict[str, Any], config: PipelineConfig) -> np.ndarray:
    return synthesize_from_params(
        params,
        frame_samples=config.frame_samples,
        hop_samples=config.hop_samples,
    )


def validate_frame_alignment(
    *,
    entry: ManifestEntry,
    pcm: np.ndarray,
    frame_count: int,
    truth_count: int,
    config: PipelineConfig,
) -> None:
    expected_sample_count = (
        (truth_count - 1) * config.hop_samples + config.frame_samples
        if truth_count > 0
        else 0
    )
    if frame_count == truth_count and len(pcm) == expected_sample_count:
        return
    if frame_count != truth_count:
        first_deviation = min(frame_count, truth_count)
    elif len(pcm) < expected_sample_count:
        first_deviation = min(frame_count, len(pcm) // max(1, config.hop_samples))
    else:
        first_deviation = truth_count
    fail(
        "E_FRAME_MISMATCH",
        "帧数、样本数与真值帧数不一致",
        field="frame_count",
        details={
            "utterance_id": entry.utterance_id,
            "first_deviation_frame": int(first_deviation),
            "frame_count": int(frame_count),
            "truth_frame_count": int(truth_count),
            "sample_count": int(len(pcm)),
            "expected_sample_count": int(expected_sample_count),
            "frame_ms": config.frame_ms,
            "hop_ms": config.hop_ms,
        },
    )


def pcm_sha256(pcm: np.ndarray) -> str:
    return pcm_hash(pcm)

