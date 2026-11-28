"""Independent scoring reference.

This module intentionally does not import app.features, app.scoring, or
app.ranking. It recomputes every statistic and score directly from raw arrays.
"""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from app.config import (
    ACCELERATION_CHANNELS,
    ACTION_PROFILES,
    CANONICAL_SAMPLE_COUNT,
    CHANNELS,
    DEFAULT_PHASE_WEIGHTS,
    GYROSCOPE_CHANNELS,
    INJURY_TAG_BODY_PART,
    INJURY_TAG_SEVERITY,
    PHASE_LOAD_MULTIPLIER,
    SCORE_DECIMALS,
    SPECIALTY_ACTION_AFFINITY,
)


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _resample(values: list[float] | np.ndarray) -> np.ndarray:
    series = np.asarray(values, dtype=float).reshape(-1)
    if series.size == 0:
        return np.zeros(CANONICAL_SAMPLE_COUNT, dtype=float)
    if series.size == 1:
        return np.full(CANONICAL_SAMPLE_COUNT, float(series[0]))
    old = np.linspace(0.0, 1.0, series.size)
    new = np.linspace(0.0, 1.0, CANONICAL_SAMPLE_COUNT)
    return np.interp(new, old, series)


def _unpack(raw: Any, channels: tuple[str, str, str]) -> dict[str, np.ndarray]:
    if isinstance(raw, dict):
        return {
            channel: np.asarray(raw[channel], dtype=float).reshape(-1)
            for channel in channels
        }
    matrix = np.asarray(raw, dtype=float)
    return {
        channel: matrix[:, index]
        for index, channel in enumerate(channels)
    }


def _channel_stats(values: np.ndarray) -> dict[str, float]:
    data = _resample(values)
    mean = float(np.mean(data))
    centered = data - mean
    variance = float(np.mean(centered * centered))
    rms = float(np.sqrt(np.mean(data * data)))
    crest = float(np.max(np.abs(data))) / rms if rms > 1e-12 else 0.0
    signs = np.signbit(centered[1:]) != np.signbit(centered[:-1])
    zcr = float(np.count_nonzero(signs) / max(1, data.size - 1))
    envelope = np.abs(centered)
    if float(np.max(envelope)) <= 1e-12:
        rhythm = 0.0
    else:
        width = min(7, max(3, envelope.size // 16 * 2 + 1))
        smooth = np.convolve(
            envelope, np.ones(width, dtype=float) / width, mode="same"
        )
        smooth = smooth - float(np.mean(smooth))
        if float(np.dot(smooth, smooth)) <= 1e-12:
            rhythm = 0.0
        else:
            maximum_lag = max(8, min(48, smooth.size // 2))
            autocorrelation = np.correlate(smooth, smooth, mode="full")[
                smooth.size - 1 :
            ]
            autocorrelation = autocorrelation / autocorrelation[0]
            candidates = autocorrelation[4 : maximum_lag + 1]
            rhythm = _clip(float(np.max(candidates))) if candidates.size else 0.0
    return {
        "mean": mean,
        "variance": variance,
        "peak_factor": crest,
        "zero_crossing_rate": zcr,
        "rhythm_stability": rhythm,
    }


def _sorted_tags(profile: dict[str, Any]) -> list[str]:
    return sorted(
        {
            str(tag).strip()
            for tag in profile.get("injury_tags", [])
            if str(tag).strip()
        }
    )


def _body_parts(tags: Iterable[str]) -> list[str]:
    return sorted(
        {
            INJURY_TAG_BODY_PART[tag]
            for tag in tags
            if tag in INJURY_TAG_BODY_PART
        }
    )


def _features(
    clip: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    acceleration = _unpack(clip["acceleration"], ACCELERATION_CHANNELS)
    gyroscope = _unpack(clip["angular_velocity"], GYROSCOPE_CHANNELS)
    raw = {**acceleration, **gyroscope}
    canonical = {channel: _resample(raw[channel]) for channel in CHANNELS}
    statistics = {channel: _channel_stats(canonical[channel]) for channel in CHANNELS}
    acc_matrix = np.column_stack([canonical[c] for c in ACCELERATION_CHANNELS])
    gyro_matrix = np.column_stack([canonical[c] for c in GYROSCOPE_CHANNELS])
    acc_peak = float(np.max(np.linalg.norm(acc_matrix, axis=1)))
    gyro_peak = float(np.max(np.linalg.norm(gyro_matrix, axis=1)))
    peak_load = 100.0 * (
        0.72 * _clip(acc_peak / 26.0) + 0.28 * _clip(gyro_peak / 13.0)
    )
    tags = _sorted_tags(profile)
    return {
        "statistics": statistics,
        "rhythm_stability": float(
            np.mean([statistics[c]["rhythm_stability"] for c in CHANNELS])
        ),
        "peak_factor": float(
            np.mean([statistics[c]["peak_factor"] for c in CHANNELS])
        ),
        "peak_load": peak_load,
        "injury_tags": tags,
        "injury_parts": _body_parts(tags),
    }


def _quality(clip: dict[str, Any], features: dict[str, Any]) -> float:
    manual = float(clip["manual_quality_score"])
    stability = features["rhythm_stability"] * 100.0
    crest_fit = 100.0 * _clip(1.0 - abs(features["peak_factor"] - 1.75) / 2.1)
    duration_fit = 100.0 * _clip(
        1.0 - abs(float(clip["duration_sec"]) - 6.0) / 8.0
    )
    return (
        0.50 * manual
        + 0.28 * stability
        + 0.12 * crest_fit
        + 0.10 * duration_fit
    )


def _risk_parts(
    clip: dict[str, Any],
    profile: dict[str, Any],
    phase: str,
    features: dict[str, Any],
) -> tuple[float, list[dict[str, Any]], float]:
    action = str(clip["action_category"])
    action_profile = ACTION_PROFILES[action]
    intensity = PHASE_LOAD_MULTIPLIER[phase]
    peak_load = features["peak_load"]
    mechanical = min(
        100.0, float(action_profile["load"]) * intensity * 100.0
    )
    volume = 100.0 * _clip(float(clip["repetition_count"]) / 20.0)
    exposure = 100.0 * _clip(float(clip["duration_sec"]) / 15.0)
    score = (
        0.50 * peak_load
        + 0.20 * mechanical
        + 0.18 * volume
        + 0.12 * exposure
    )
    base = score
    tags_by_part: dict[str, list[str]] = {}
    for tag in features["injury_tags"]:
        part = INJURY_TAG_BODY_PART.get(tag)
        if part in action_profile["body_parts"]:
            tags_by_part.setdefault(part, []).append(tag)
    peak_gate = _clip((peak_load - 42.0) / 36.0)
    adjustments: list[dict[str, Any]] = []
    for part in sorted(tags_by_part):
        matched = sorted(tags_by_part[part])
        severity = max(INJURY_TAG_SEVERITY.get(tag, 0.5) for tag in matched)
        proposed = (
            (5.0 + 24.0 * severity)
            * (0.70 + 0.30 * float(action_profile["load"]))
            * peak_gate
            * intensity
        )
        before = score
        score = min(100.0, score + proposed)
        adjustments.append(
            {
                "body_part": part,
                "matched_tags": matched,
                "action_category": action,
                "peak_load": float(peak_load),
                "adjustment_delta": float(score - before),
                "before_risk": float(before),
                "after_risk": float(score),
            }
        )
    return score, [
        adjustment
        for adjustment in adjustments
        if adjustment["adjustment_delta"] > 0.0
    ], base


def _profile_score(
    clip: dict[str, Any],
    profile: dict[str, Any],
    phase: str,
    features: dict[str, Any],
) -> float:
    action = str(clip["action_category"])
    specialty = str(profile["specialty"])
    action_profile = ACTION_PROFILES[action]
    affinity = float(SPECIALTY_ACTION_AFFINITY.get(specialty, {}).get(action, 0.36))
    demand = float(action_profile["demand_years"])
    years = float(profile["training_years"])
    experience_fit = 100.0 * _clip(
        1.0 - abs(years - demand) / max(12.0, demand + 5.0)
    )
    injured = features["injury_parts"]
    if injured:
        stressed = set(injured) & set(action_profile["body_parts"])
        injury_fit = 100.0 * (1.0 - 0.55 * len(stressed) / len(injured))
    else:
        injury_fit = 100.0
    target = {
        "baseline": 0.65,
        "build": 0.78,
        "deload": 0.45,
        "peak": 0.92,
        "recovery": 0.38,
    }[phase]
    phase_fit = 100.0 * _clip(
        1.0 - abs(float(action_profile["load"]) - target) / 0.55
    )
    return (
        0.40 * affinity * 100.0
        + 0.24 * experience_fit
        + 0.22 * injury_fit
        + 0.14 * phase_fit
    )


def score_candidate_reference(
    clip: dict[str, Any],
    profile: dict[str, Any],
    phase: str,
    weights: dict[str, float],
) -> dict[str, Any]:
    features = _features(clip, profile)
    quality = _quality(clip, features)
    risk, injury_adjustments, base_risk = _risk_parts(
        clip, profile, phase, features
    )
    profile_score = _profile_score(clip, profile, phase, features)
    risk_safety = 100.0 - risk
    fused = (
        weights["quality"] * quality
        + weights["risk"] * risk_safety
        + weights["profile"] * profile_score
    )
    return {
        "clip_id": clip["clip_id"],
        "manual_quality_score": float(clip["manual_quality_score"]),
        "metadata": dict(clip.get("metadata", {})),
        "scores": {
            "quality": float(quality),
            "load_risk": float(risk),
            "risk_safety": float(risk_safety),
            "profile_match": float(profile_score),
            "fused": float(fused),
        },
        "base_load_risk": float(base_risk),
        "injury_risk_uplift": injury_adjustments,
    }


def rank_request_reference(
    *,
    request_id: str,
    athlete_id: str,
    phase: str,
    candidates: Iterable[dict[str, Any]],
    profile: dict[str, Any],
    weights: dict[str, float] | None = None,
    prior_ranks: dict[str, int] | None = None,
) -> dict[str, Any]:
    effective_weights = dict(weights or DEFAULT_PHASE_WEIGHTS[phase])
    scored = [
        score_candidate_reference(clip, profile, phase, effective_weights)
        for clip in candidates
    ]
    scored.sort(
        key=lambda item: (
            -round(item["scores"]["fused"], SCORE_DECIMALS),
            item["clip_id"],
        )
    )
    prior_ranks = dict(prior_ranks or {})
    ranking: list[dict[str, Any]] = []
    for rank, item in enumerate(scored, start=1):
        prior_rank = prior_ranks.get(item["clip_id"], rank)
        ranking.append(
            {
                **item,
                "rank": rank,
                "prior_rank": prior_rank,
                "rank_delta": rank - prior_rank,
            }
        )
    return {
        "request_id": request_id,
        "athlete_id": athlete_id,
        "phase": phase,
        "weights": effective_weights,
        "ranked_order": [item["clip_id"] for item in ranking],
        "ranking": ranking,
        "injury_uplift_candidates": [
            item["clip_id"] for item in ranking if item["injury_risk_uplift"]
        ],
    }


def score_map(result: dict[str, Any]) -> dict[str, dict[str, float]]:
    return {
        item["clip_id"]: {
            key: float(value) for key, value in item["scores"].items()
        }
        for item in result["ranking"]
    }
