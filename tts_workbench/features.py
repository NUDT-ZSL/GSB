from __future__ import annotations

import hashlib
import math
from typing import Any

import numpy as np

from .config import AcousticConfig
from .model import FrameParam
from .util import canonical_json, sha256_bytes, sha256_json


FEATURE_ALGORITHM_VERSION = 1


def audio_segment_fingerprint(
    audio: np.ndarray,
    start_sample: int,
    length_samples: int,
) -> str:
    end = start_sample + length_samples
    if start_sample >= audio.size:
        segment = np.zeros(0, dtype=np.int16)
    else:
        segment = audio[start_sample:min(end, audio.size)]
    if segment.size < length_samples:
        padded = np.zeros(length_samples, dtype=np.int16)
        padded[: segment.size] = segment
        segment = padded
    return sha256_bytes(np.ascontiguousarray(segment, dtype="<i2").tobytes())


def voicing_decision(frame: FrameParam, config: AcousticConfig) -> dict[str, Any]:
    lhs = frame.voicing_score
    threshold = config.voicing_threshold
    score_voiced = lhs >= threshold
    if frame.voiced_hint is None:
        voiced = score_voiced
        source = "score_threshold"
    else:
        voiced = bool(frame.voiced_hint)
        source = "frame_override"
    return {
        "score": lhs,
        "threshold": threshold,
        "operator": ">=",
        "score_result": score_voiced,
        "voiced": voiced,
        "tie_rule": "score == threshold -> voiced",
        "source": source,
        "comparison_text": f"{lhs:.12f} >= {threshold:.12f} -> {str(score_voiced).lower()}",
    }


def quantize_f0(
    raw_f0_hz: float, config: AcousticConfig
) -> tuple[float, int, bool, bool]:
    step = config.f0_quantization_hz
    position = (raw_f0_hz - config.f0_min_hz) / step
    index = int(math.floor(position + 0.5))
    level_count = int(math.floor((config.f0_max_hz - config.f0_min_hz) / step + 1e-12)) + 1
    clamped_index = min(max(index, 0), level_count - 1)
    quantized = config.f0_min_hz + clamped_index * step
    return quantized, clamped_index, index < 0, index > level_count - 1


def compute_frame_feature(
    *,
    frame: FrameParam,
    f0_scale: float,
    config: AcousticConfig,
    audio_fingerprint: str,
    f0_edit_digest: str,
    utterance_id: str,
    phone: str,
    global_frame_start: int,
) -> tuple[dict[str, Any], str]:
    raw_f0 = frame.f0_hz * f0_scale
    decision = voicing_decision(frame, config)
    voiced = decision["voiced"]
    forced_unvoiced_reason: str | None = None
    if voiced and raw_f0 <= 0.0:
        voiced = False
        forced_unvoiced_reason = "f0_raw <= 0"

    if voiced:
        f0_hz, f0_index, clamped_low, clamped_high = quantize_f0(raw_f0, config)
    else:
        f0_hz, f0_index, clamped_low, clamped_high = 0.0, -1, False, False

    nyquist = config.sample_rate / 2.0
    bands: list[float] = []
    active_harmonics: list[int] = []
    for band_index in range(config.spectral_bands):
        harmonic = band_index + 1
        frequency = f0_hz * harmonic
        if voiced and frequency <= nyquist:
            amplitude = (1.0 / (harmonic**1.20)) * (1.0 + 0.04 * ((band_index % 5) - 2))
            bands.append(float(amplitude))
            active_harmonics.append(harmonic)
        else:
            bands.append(0.0)

    key_payload = {
        "algorithm": FEATURE_ALGORITHM_VERSION,
        "acoustic_hash": config.acoustic_hash,
        "utterance_id": utterance_id,
        "phone": phone,
        "audio_frame_sha256": audio_fingerprint,
        "f0_edit_digest": f0_edit_digest,
        "global_frame_start": global_frame_start,
        "frame_content": {
            "f0_raw_hz": raw_f0,
            "energy": frame.energy,
            "noise_floor": frame.noise_floor,
            "voicing_score": frame.voicing_score,
            "voiced_hint": frame.voiced_hint,
        },
        "f0_scale": f0_scale,
    }
    key = sha256_json(key_payload)
    noise_seed = int(
        sha256_json(
            {
                "algorithm": FEATURE_ALGORITHM_VERSION,
                "sample_rate": config.sample_rate,
                "frame_length_samples": config.frame_length_samples,
                "utterance_id": utterance_id,
                "phone": phone,
                "global_frame_start": global_frame_start,
                "f0_hz": f0_hz,
            }
        )[:16],
        16,
    )
    feature = {
        "key": key,
        "global_frame_start": global_frame_start,
        "f0_raw_hz": raw_f0,
        "f0_hz": f0_hz,
        "f0_quant_index": f0_index,
        "f0_clamped_low": clamped_low,
        "f0_clamped_high": clamped_high,
        "voiced": voiced,
        "voicing_decision": decision,
        "forced_unvoiced_reason": forced_unvoiced_reason,
        "spectral_bands": bands,
        "active_harmonics": active_harmonics,
        "nyquist_hz": nyquist,
        "noise_floor": frame.noise_floor,
        "noise_seed": noise_seed,
    }
    return feature, key


def energy_frame_key(
    *,
    feature_key: str,
    effective_energy: float,
    target_dbfs: float,
    global_frame_start: int,
    config: AcousticConfig,
    energy_edit_digest: str,
) -> str:
    return sha256_json(
        {
            "algorithm": FEATURE_ALGORITHM_VERSION,
            "acoustic_hash": config.acoustic_hash,
            "feature_key": feature_key,
            "energy_edit_digest": energy_edit_digest,
            "effective_energy": effective_energy,
            "target_dbfs": target_dbfs,
            "global_frame_start": global_frame_start,
            "frame_length_samples": config.frame_length_samples,
            "sample_rate": config.sample_rate,
            "quantized_rounding": "fixed_q16_half_up_symmetric",
        }
    )


def _counter_noise(seed: int, global_start: int, length: int) -> np.ndarray:
    indices = np.arange(length, dtype=np.uint64) + np.uint64(global_start)
    z = indices * np.uint64(0x9E3779B97F4A7C15) + np.uint64(seed & 0xFFFFFFFF)
    z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
    z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
    z ^= z >> np.uint64(31)
    # Keep the low 24 bits in the signed fixed-point input domain.
    unit = (z & np.uint64(0xFFFFFF)).astype(np.float64) / float(0x800000)
    return unit - 1.0


def _round_half_up_symmetric(values: np.ndarray) -> np.ndarray:
    return np.where(values >= 0, np.floor(values + 0.5), -np.floor(-values + 0.5)).astype(
        np.int64
    )


def synthesize_energy_normalized_frame(
    *,
    feature: dict[str, Any],
    effective_energy: float,
    config: AcousticConfig,
    global_frame_start: int,
) -> dict[str, Any]:
    length = config.frame_length_samples
    sample_indices = np.arange(length, dtype=np.float64) + float(global_frame_start)
    waveform = np.zeros(length, dtype=np.float64)
    if feature["voiced"]:
        f0_hz = float(feature["f0_hz"])
        for harmonic, amplitude in zip(
            feature["active_harmonics"], feature["spectral_bands"], strict=True
        ):
            frequency = f0_hz * harmonic
            waveform += amplitude * np.sin(
                2.0 * math.pi * frequency * sample_indices / config.sample_rate
            )

    seed = int(feature["noise_seed"])
    noise = _counter_noise(seed, global_frame_start, length)
    waveform += float(feature["noise_floor"]) * 0.20 * noise
    raw_samples = _round_half_up_symmetric(waveform * 32767.0)

    sum_squares = int(np.dot(raw_samples, raw_samples))
    raw_rms = math.isqrt(sum_squares // max(length, 1))
    clamped_energy = min(1.0, max(0.0, effective_energy))
    if clamped_energy <= 0.0:
        target_dbfs = -120.0
        target_rms = 0
    else:
        target_dbfs = config.energy_target_dbfs + 20.0 * math.log10(clamped_energy)
        target_rms = int(math.floor(32767.0 * (10.0 ** (target_dbfs / 20.0)) + 0.5))
    target_rms = max(0, min(32767, target_rms))

    if raw_rms > 0 and target_rms > 0:
        gain_q16 = (target_rms << 16) // raw_rms
    else:
        gain_q16 = 0
    scaled = _round_half_up_symmetric(raw_samples.astype(np.float64) * gain_q16 / 65536.0)
    clipped = int(np.count_nonzero((scaled < -32768) | (scaled > 32767)))
    pcm = np.clip(scaled, -32768, 32767).astype(np.int16)
    return {
        "pcm": np.ascontiguousarray(pcm),
        "raw_rms_q15": raw_rms,
        "target_rms_q15": target_rms,
        "gain_q16": gain_q16,
        "target_dbfs": target_dbfs,
        "clipped_samples": clipped,
        "effective_energy": clamped_energy,
        "pcm_sha256": hashlib.sha256(pcm.astype("<i2").tobytes()).hexdigest(),
    }


def feature_summary(feature: dict[str, Any]) -> dict[str, Any]:
    bands = feature["spectral_bands"]
    return {
        "f0_raw_hz": feature["f0_raw_hz"],
        "f0_hz": feature["f0_hz"],
        "f0_quant_index": feature["f0_quant_index"],
        "voiced": feature["voiced"],
        "spectral_band_energy": float(sum(bands)),
        "active_harmonics": len(feature["active_harmonics"]),
        "nyquist_hz": feature["nyquist_hz"],
    }


def stable_task_key(utterance_id: str, phoneme_index: int, frame_start: int) -> tuple[str, int, int]:
    return (utterance_id, phoneme_index, frame_start)


def interval_cache_key(frame_keys: list[str], energy_keys: list[str], config: AcousticConfig) -> str:
    return sha256_json(
        {
            "algorithm": FEATURE_ALGORITHM_VERSION,
            "acoustic_hash": config.acoustic_hash,
            "frame_keys": frame_keys,
            "energy_keys": energy_keys,
        }
    )


def concat_cache_key(
    *,
    utterance_id: str,
    config: AcousticConfig,
    frame_entries: list[dict[str, Any]],
) -> str:
    return sha256_json(
        {
            "algorithm": FEATURE_ALGORITHM_VERSION,
            "acoustic_hash": config.acoustic_hash,
            "utterance_id": utterance_id,
            "frame_entries": frame_entries,
        }
    )


def pcm_sha256(pcm: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(pcm, dtype="<i2").tobytes()).hexdigest()


def canonical_key_payload(value: Any) -> str:
    return canonical_json(value)
