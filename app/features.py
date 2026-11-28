from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np

from app.config import (
    ACCELERATION_CHANNELS,
    ACTION_CATEGORY_CODE,
    ACTION_CATEGORIES,
    BODY_PART_CODE,
    BODY_PARTS,
    CANONICAL_SAMPLE_COUNT,
    CHANNELS,
    GYROSCOPE_CHANNELS,
    INJURY_TAG_BODY_PART,
    SPECIALTY_CODE,
    SPECIALTIES,
)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def canonical_resample(values: list[float] | np.ndarray) -> np.ndarray:
    series = np.asarray(values, dtype=float).reshape(-1)
    if series.size == 0:
        return np.zeros(CANONICAL_SAMPLE_COUNT, dtype=float)
    if not np.all(np.isfinite(series)):
        raise ValueError("传感器序列包含非有限数值")
    if series.size == 1:
        return np.full(CANONICAL_SAMPLE_COUNT, float(series[0]))
    source_grid = np.linspace(0.0, 1.0, series.size)
    target_grid = np.linspace(0.0, 1.0, CANONICAL_SAMPLE_COUNT)
    return np.interp(target_grid, source_grid, series)


def channel_map(raw: Any, names: tuple[str, str, str]) -> dict[str, np.ndarray]:
    if isinstance(raw, dict):
        missing = [name for name in names if name not in raw]
        if missing:
            raise ValueError(f"缺少通道: {', '.join(missing)}")
        return {name: np.asarray(raw[name], dtype=float).reshape(-1) for name in names}
    array = np.asarray(raw, dtype=float)
    if array.ndim != 2 or array.shape[1] != 3:
        raise ValueError("通道数据必须是包含 3 列的数组，或 ax/ay/az 键的字典")
    return {name: array[:, index] for index, name in enumerate(names)}


def clip_channel_map(clip: dict[str, Any]) -> dict[str, np.ndarray]:
    acceleration = channel_map(clip["acceleration"], ACCELERATION_CHANNELS)
    angular_velocity = channel_map(clip["angular_velocity"], GYROSCOPE_CHANNELS)
    return {**acceleration, **angular_velocity}


def _rhythm_stability(series: np.ndarray) -> float:
    centered = series - float(np.mean(series))
    envelope = np.abs(centered)
    if float(np.max(envelope)) <= 1e-12:
        return 0.0
    kernel_size = min(7, max(3, envelope.size // 16 * 2 + 1))
    kernel = np.ones(kernel_size, dtype=float) / kernel_size
    smooth = np.convolve(envelope, kernel, mode="same")
    smooth = smooth - float(np.mean(smooth))
    energy = float(np.dot(smooth, smooth))
    if energy <= 1e-12:
        return 0.0
    max_lag = max(8, min(48, smooth.size // 2))
    autocorrelation = np.correlate(smooth, smooth, mode="full")[smooth.size - 1 :]
    autocorrelation = autocorrelation / autocorrelation[0]
    candidates = autocorrelation[4 : max_lag + 1]
    if candidates.size == 0:
        return 0.0
    return clamp(float(np.max(candidates)))


def channel_statistics(series: np.ndarray) -> dict[str, float]:
    canonical = canonical_resample(series)
    mean = float(np.mean(canonical))
    centered = canonical - mean
    variance = float(np.mean(centered * centered))
    rms = float(np.sqrt(np.mean(canonical * canonical)))
    peak = float(np.max(np.abs(canonical)))
    peak_factor = peak / rms if rms > 1e-12 else 0.0
    sign_changes = np.count_nonzero(np.signbit(centered[1:]) != np.signbit(centered[:-1]))
    zero_crossing_rate = float(sign_changes / max(1, canonical.size - 1))
    return {
        "mean": mean,
        "variance": variance,
        "peak_factor": peak_factor,
        "zero_crossing_rate": zero_crossing_rate,
        "rhythm_stability": _rhythm_stability(canonical),
    }


def unique_injury_tags(tags: list[str] | tuple[str, ...] | None) -> list[str]:
    cleaned = {
        str(tag).strip()
        for tag in (tags or [])
        if tag is not None and str(tag).strip()
    }
    return sorted(cleaned)


def injury_body_parts(tags: list[str] | tuple[str, ...] | None) -> list[str]:
    parts = {
        INJURY_TAG_BODY_PART[tag]
        for tag in unique_injury_tags(tags)
        if tag in INJURY_TAG_BODY_PART
    }
    return [part for part in BODY_PARTS if part in parts]


def one_hot(value: str, vocabulary: tuple[str, ...]) -> list[int]:
    return [1 if item == value else 0 for item in vocabulary]


def alignment_representation(clip: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    action = str(clip["action_category"])
    specialty = str(profile["specialty"])
    tags = unique_injury_tags(profile.get("injury_tags", []))
    injured_parts = injury_body_parts(tags)

    action_vector = one_hot(action, tuple(sorted(ACTION_CATEGORIES)))
    specialty_vector = one_hot(specialty, tuple(sorted(SPECIALTIES)))
    injury_vector = [1 if part in injured_parts else 0 for part in sorted(BODY_PARTS)]
    combined = action_vector + specialty_vector + injury_vector
    signature_source = json.dumps(
        {
            "action": action,
            "specialty": specialty,
            "injury_body_parts": injured_parts,
            "combined": combined,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return {
        "action_category": action,
        "specialty": specialty,
        "injury_tags": tags,
        "injury_body_parts": injured_parts,
        "action_code": ACTION_CATEGORY_CODE.get(action, -1),
        "specialty_code": SPECIALTY_CODE.get(specialty, -1),
        "injury_body_codes": [BODY_PART_CODE[part] for part in injured_parts],
        "action_vector": action_vector,
        "specialty_vector": specialty_vector,
        "injury_vector": injury_vector,
        "combined_vector": combined,
        "signature": hashlib.sha256(signature_source.encode("utf-8")).hexdigest(),
    }


def extract_features(
    clip: dict[str, Any],
    profile: dict[str, Any],
    phase: str,
) -> dict[str, Any]:
    channels = clip_channel_map(clip)
    canonical = {name: canonical_resample(channels[name]) for name in CHANNELS}
    stats = {name: channel_statistics(canonical[name]) for name in CHANNELS}

    acceleration_matrix = np.column_stack([canonical[name] for name in ACCELERATION_CHANNELS])
    gyroscope_matrix = np.column_stack([canonical[name] for name in GYROSCOPE_CHANNELS])
    acceleration_magnitude = np.linalg.norm(acceleration_matrix, axis=1)
    gyroscope_magnitude = np.linalg.norm(gyroscope_matrix, axis=1)
    acceleration_peak = float(np.max(acceleration_magnitude))
    gyroscope_peak = float(np.max(gyroscope_magnitude))
    peak_load = 100.0 * (
        0.72 * clamp(acceleration_peak / 26.0)
        + 0.28 * clamp(gyroscope_peak / 13.0)
    )

    summary = {
        "mean_abs": float(np.mean([abs(stats[name]["mean"]) for name in CHANNELS])),
        "variance_mean": float(np.mean([stats[name]["variance"] for name in CHANNELS])),
        "peak_factor": float(np.mean([stats[name]["peak_factor"] for name in CHANNELS])),
        "zero_crossing_rate": float(
            np.mean([stats[name]["zero_crossing_rate"] for name in CHANNELS])
        ),
        "rhythm_stability": float(
            np.mean([stats[name]["rhythm_stability"] for name in CHANNELS])
        ),
        "acceleration_peak": acceleration_peak,
        "gyroscope_peak": gyroscope_peak,
        "peak_load": peak_load,
    }
    return {
        "phase": phase,
        "channels": stats,
        "summary": summary,
        "alignment": alignment_representation(clip, profile),
        "sample_counts": {
            name: int(len(channels[name])) for name in CHANNELS
        },
        "canonical_sample_count": CANONICAL_SAMPLE_COUNT,
    }
