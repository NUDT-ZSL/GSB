"""Exact framing and deterministic frame-level acoustic features."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence

import numpy as np

from .canonical import file_sha256, sha256_hex
from .config import EngineConfig
from .errors import DiagnosticError, ErrorCode


def load_pcm(path: str | Path) -> np.ndarray:
    raw = Path(path).read_bytes()
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    return pcm


def pcm_bytes_hash(path: str | Path) -> str:
    return file_sha256(path)


def frame_boundaries(
    pcm_samples: int, config: EngineConfig
) -> List[tuple[int, int]]:
    frame_samples = config.frame_samples
    hop_samples = config.hop_samples
    if pcm_samples < frame_samples:
        return []
    count = 1 + (pcm_samples - frame_samples) // hop_samples
    return [
        (index * hop_samples, index * hop_samples + frame_samples)
        for index in range(count)
    ]


def zero_crossing_rate(frame: np.ndarray) -> float:
    if frame.size < 2:
        return 0.0
    signs = np.signbit(frame)
    crossings = int(np.count_nonzero(signs[:-1] != signs[1:]))
    return crossings / float(frame.size - 1)


def fundamental_frequency_candidates(
    frame: np.ndarray, sample_rate: int, *, voiced_rms_floor: float = 0.010
) -> List[Dict[str, float | int]]:
    """Return up to three autocorrelation F0 candidates in deterministic order."""
    if frame.size < 3 or float(np.sqrt(np.mean(frame * frame))) < voiced_rms_floor:
        return []
    centered = frame - float(np.mean(frame))
    total = float(np.dot(centered, centered))
    if total <= 1e-12:
        return []

    autocorrelation = np.correlate(centered, centered, mode="full")[frame.size - 1 :]
    autocorrelation = autocorrelation / total
    lag_min = max(2, sample_rate // 400)
    lag_max = min(sample_rate // 70, frame.size - 2)
    if lag_max <= lag_min:
        return []

    candidates: List[Dict[str, float | int]] = []
    for lag in range(lag_min, lag_max + 1):
        score = float(autocorrelation[lag])
        if score < 0.18:
            continue
        is_peak = (
            score >= float(autocorrelation[lag - 1])
            and score >= float(autocorrelation[lag + 1])
        )
        if not is_peak:
            continue
        candidates.append(
            {
                "f0_hz": round(sample_rate / float(lag), 6),
                "lag_samples": int(lag),
                "score": round(score, 9),
            }
        )

    candidates.sort(key=lambda item: (-float(item["score"]), int(item["lag_samples"])))
    return candidates[:3]


def extract_features(
    pcm: np.ndarray,
    config: EngineConfig,
    *,
    pcm_hash: str | None = None,
    expected_frame_count: int | None = None,
) -> List[Dict[str, Any]]:
    """Extract one stable feature record for every exact analysis frame."""
    boundaries = frame_boundaries(int(pcm.size), config)
    if expected_frame_count is not None and len(boundaries) != int(expected_frame_count):
        raise DiagnosticError(
            ErrorCode.FRAME_COUNT_MISMATCH,
            "feature frame count does not match truth/manifest",
            {
                "feature_frame_count": len(boundaries),
                "expected_frame_count": expected_frame_count,
                "pcm_samples": int(pcm.size),
                "frame_samples": config.frame_samples,
                "hop_samples": config.hop_samples,
            },
        )

    sample_rate = int(config["sample_rate"])
    voiced_floor = float(config["anomaly"]["energy_floor"]) * 0.8
    records: List[Dict[str, Any]] = []
    previous_energy: float | None = None
    previous_zcr: float | None = None

    for frame_index, (start, end) in enumerate(boundaries):
        frame = pcm[start:end]
        energy = float(np.sum(frame * frame, dtype=np.float64))
        rms = float(np.sqrt(np.mean(frame * frame, dtype=np.float64)))
        zcr = zero_crossing_rate(frame)
        candidates = fundamental_frequency_candidates(
            frame, sample_rate, voiced_rms_floor=voiced_floor
        )
        f0 = float(candidates[0]["f0_hz"]) if candidates else 0.0
        energy_delta = 0.0 if previous_energy is None else energy - previous_energy
        energy_rate = 0.0
        if previous_energy is not None:
            denominator = max(abs(previous_energy), 1e-9)
            energy_rate = energy_delta / denominator
        zcr_gradient = 0.0 if previous_zcr is None else zcr - previous_zcr

        record = {
            "frame_index": frame_index,
            "start_sample": int(start),
            "end_sample": int(end),
            "start_ms": int(start * 1000 // sample_rate),
            "end_ms": int(end * 1000 // sample_rate),
            "energy": round(energy, 12),
            "rms": round(rms, 12),
            "zcr": round(zcr, 12),
            "f0_hz": round(f0, 6),
            "f0_candidates": candidates,
            "energy_delta": round(energy_delta, 12),
            "energy_change_rate": round(float(energy_rate), 12),
            "zcr_gradient": round(float(zcr_gradient), 12),
            "pcm_sha256": pcm_hash,
            "feature_params_sha256": sha256_hex(
                {
                    "sample_rate": sample_rate,
                    "frame_ms": int(config["frame_ms"]),
                    "hop_ms": int(config["hop_ms"]),
                    "frame_samples": config.frame_samples,
                    "hop_samples": config.hop_samples,
                }
            ),
        }
        records.append(record)
        previous_energy = energy
        previous_zcr = zcr

    return records


def feature_matrix(features: Sequence[Dict[str, Any]]) -> np.ndarray:
    matrix = np.zeros((len(features), 4), dtype=np.float64)
    for index, item in enumerate(features):
        matrix[index, 0] = float(item["energy"])
        matrix[index, 1] = float(item["rms"])
        matrix[index, 2] = float(item["zcr"])
        matrix[index, 3] = float(item["f0_hz"])
    return matrix


def feature_memory_bytes(features: Sequence[Dict[str, Any]]) -> int:
    from .canonical import deep_sizeof

    return deep_sizeof(list(features)) + feature_matrix(features).nbytes
