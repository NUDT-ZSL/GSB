"""Exact sample-index framing and deterministic acoustic feature scoring."""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

from .config import EngineConfig
from .util import clamp, rounded, sha256_bytes, sha256_json


PCM_DTYPE = np.dtype("<i2")


def pcm_content_hash(pcm: np.ndarray, sample_rate: int) -> str:
    contiguous = np.ascontiguousarray(np.asarray(pcm, dtype=PCM_DTYPE))
    digest = sha256_bytes(contiguous.tobytes())
    return sha256_json({"sample_rate": sample_rate, "pcm_sha256": digest})


def feature_cache_key(pcm_hash: str, sample_rate: int, config: EngineConfig) -> str:
    return sha256_json(
        {
            "pcm_content_key": pcm_hash,
            "sample_rate": sample_rate,
            "frame_ms": config.frame_ms,
            "hop_ms": config.hop_ms,
            "feature_signature": config.feature_signature(),
        }
    )


def complete_frame_count(sample_count: int, frame_samples: int, hop_samples: int) -> int:
    if sample_count < frame_samples:
        return 0
    return 1 + (sample_count - frame_samples) // hop_samples


def _autocorrelation_f0(
    frame: np.ndarray,
    sample_rate: int,
    config: EngineConfig,
) -> tuple[float, float]:
    n = int(frame.shape[0])
    feature = config.feature
    min_lag = max(1, int(math.floor(sample_rate / float(feature["max_f0_hz"]))))
    max_lag = min(int(math.ceil(sample_rate / float(feature["min_f0_hz"]))), n - 2)
    if max_lag < min_lag:
        return 0.0, 0.0

    centered = frame - math.fsum(float(value) for value in frame) / n
    window = np.hanning(n)
    windowed = centered * window
    energy = math.fsum(float(value) * float(value) for value in windowed)
    if energy <= config.subnormal_guard:
        return 0.0, 0.0

    best_lag = 0
    best_score = -1.0
    for lag in range(min_lag, max_lag + 1):
        numerator = math.fsum(
            float(windowed[index]) * float(windowed[index + lag])
            for index in range(n - lag)
        )
        score = numerator / energy
        if score > best_score:
            best_score = score
            best_lag = lag
    if best_lag == 0 or best_score < float(feature["autocorrelation_floor"]):
        return 0.0, rounded(best_score, config.score_decimals, config.subnormal_guard)
    return sample_rate / best_lag, rounded(best_score, config.score_decimals, config.subnormal_guard)


def analyze_frame(
    pcm: np.ndarray,
    frame_index: int,
    sample_rate: int,
    config: EngineConfig,
) -> dict[str, Any]:
    frame_samples = config.frame_samples(sample_rate)
    hop_samples = config.hop_samples(sample_rate)
    start = frame_index * hop_samples
    end = start + frame_samples
    if end > int(pcm.shape[0]):
        raise ValueError(f"frame {frame_index} is not complete")

    raw = np.asarray(pcm[start:end], dtype=PCM_DTYPE)
    frame = raw.astype(np.float64) / 32768.0
    feature = config.feature
    decimals = config.score_decimals

    rms = math.sqrt(
        math.fsum(float(value) * float(value) for value in frame) / frame_samples
    )
    if rms <= config.subnormal_guard:
        energy_db = float(feature["energy_floor_db"])
    else:
        energy_db = 20.0 * math.log10(rms)
        energy_db = max(float(feature["energy_floor_db"]), min(float(feature["energy_ceiling_db"]), energy_db))

    signs = np.signbit(raw)
    crossing_count = int(np.count_nonzero(signs[1:] != signs[:-1]))
    zcr = crossing_count / max(1, frame_samples - 1)
    f0_hz, autocorr = _autocorrelation_f0(frame, sample_rate, config)

    energy_norm = clamp(
        (energy_db - float(feature["energy_floor_db"]))
        / (float(feature["energy_ceiling_db"]) - float(feature["energy_floor_db"])),
        0.0,
        1.0,
    )
    energy_component = 2.0 * energy_norm - 1.0
    if f0_hz > 0.0:
        f0_norm = clamp(
            (f0_hz - float(feature["min_f0_hz"]))
            / (float(feature["max_f0_hz"]) - float(feature["min_f0_hz"])),
            0.0,
            1.0,
        )
    else:
        f0_norm = 0.0
    f0_component = 2.0 * f0_norm - 1.0
    zcr_norm = clamp(zcr / float(feature["zcr_reference"]), 0.0, 1.0)
    zcr_component = 2.0 * zcr_norm - 1.0

    valence = 0.45 * energy_component + 0.40 * f0_component - 0.15 * zcr_component
    arousal = 0.55 * energy_component + 0.25 * f0_component + 0.20 * zcr_component
    valence = rounded(clamp(valence, -1.0, 1.0), decimals, config.subnormal_guard)
    arousal = rounded(clamp(arousal, -1.0, 1.0), decimals, config.subnormal_guard)
    weight = rounded(max(rms, 1e-9), decimals, config.subnormal_guard)

    return {
        "frame_index": frame_index,
        "start_sample": start,
        "end_sample": end,
        "valid_samples": frame_samples,
        "energy_rms": rounded(rms, decimals, config.subnormal_guard),
        "energy_db": rounded(energy_db, decimals, config.subnormal_guard),
        "zero_crossing_rate": rounded(zcr, decimals, config.subnormal_guard),
        "f0_hz": rounded(f0_hz, decimals, config.subnormal_guard),
        "autocorrelation_peak": autocorr,
        "valence": valence,
        "arousal": arousal,
        "is_speech": bool(energy_db >= float(feature["speech_energy_db"])),
        "frame_weight": weight,
    }


def extract_features(
    pcm: np.ndarray,
    sample_rate: int,
    config: EngineConfig,
    frame_limit: int | None = None,
) -> list[dict[str, Any]]:
    array = np.asarray(pcm, dtype=PCM_DTYPE)
    frame_samples = config.frame_samples(sample_rate)
    hop_samples = config.hop_samples(sample_rate)
    count = complete_frame_count(int(array.shape[0]), frame_samples, hop_samples)
    if frame_limit is not None:
        count = min(count, max(0, int(frame_limit)))
    return [
        analyze_frame(array, frame_index, sample_rate, config)
        for frame_index in range(count)
    ]


def validate_speakers(
    speakers: Sequence[str],
    allowed_speakers: Sequence[str],
    frame_count: int,
) -> None:
    from .errors import E_SPEAKER_LABEL_INVALID, EngineError

    allowed = {str(item) for item in allowed_speakers}
    if len(speakers) != frame_count:
        raise EngineError(
            E_SPEAKER_LABEL_INVALID,
            "speaker label count does not match frame count",
            frame_index=min(len(speakers), frame_count),
            expected_frames=frame_count,
            actual_labels=len(speakers),
        )
    for frame_index, speaker in enumerate(speakers):
        if str(speaker) not in allowed:
            raise EngineError(
                E_SPEAKER_LABEL_INVALID,
                "speaker label is outside the declared speaker set",
                frame_index=frame_index,
                speaker_id=str(speaker),
                allowed_speakers=sorted(allowed),
            )
