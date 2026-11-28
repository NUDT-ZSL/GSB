from __future__ import annotations

from typing import Any

from app.config import (
    ACTION_PROFILES,
    DEFAULT_PHASE_WEIGHTS,
    INJURY_TAG_BODY_PART,
    INJURY_TAG_SEVERITY,
    PHASE_LOAD_MULTIPLIER,
    SPECIALTY_ACTION_AFFINITY,
    validate_weights,
)
from app.features import (
    clamp,
    extract_features,
    injury_body_parts,
    unique_injury_tags,
)

PHASE_TARGET_LOAD = {
    "baseline": 0.65,
    "build": 0.78,
    "deload": 0.45,
    "peak": 0.92,
    "recovery": 0.38,
}


def _feature(
    key: str,
    label: str,
    component: float,
    weight: float,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "component": float(component),
        "weight": float(weight),
        "contribution": float(weight * component),
    }


def _quality_score(clip: dict[str, Any], features: dict[str, Any]) -> dict[str, Any]:
    summary = features["summary"]
    manual = float(clip["manual_quality_score"])
    stability = float(summary["rhythm_stability"]) * 100.0
    crest_quality = 100.0 * clamp(1.0 - abs(float(summary["peak_factor"]) - 1.75) / 2.1)
    duration_fit = 100.0 * clamp(1.0 - abs(float(clip["duration_sec"]) - 6.0) / 8.0)
    route_features = [
        _feature("manual_quality", "人工动作质量", manual, 0.50),
        _feature("rhythm_stability", "重复节奏稳定度", stability, 0.28),
        _feature("peak_factor_fit", "峰值因子合理度", crest_quality, 0.12),
        _feature("duration_fit", "片段时长适配度", duration_fit, 0.10),
    ]
    return {
        "score": sum(item["contribution"] for item in route_features),
        "features": route_features,
    }


def _injury_adjustments(
    clip: dict[str, Any],
    profile: dict[str, Any],
    phase: str,
    peak_load: float,
) -> list[dict[str, Any]]:
    action = str(clip["action_category"])
    action_profile = ACTION_PROFILES[action]
    action_parts = set(action_profile["body_parts"])
    tags = unique_injury_tags(profile.get("injury_tags", []))
    tags_by_part: dict[str, list[str]] = {}
    for tag in tags:
        part = INJURY_TAG_BODY_PART.get(tag)
        if part is None or part not in action_parts:
            continue
        tags_by_part.setdefault(part, []).append(tag)

    phase_intensity = PHASE_LOAD_MULTIPLIER[phase]
    peak_gate = clamp((float(peak_load) - 42.0) / 36.0)
    adjustments: list[dict[str, Any]] = []
    for part in sorted(tags_by_part):
        matched_tags = sorted(tags_by_part[part])
        severity = max(INJURY_TAG_SEVERITY.get(tag, 0.5) for tag in matched_tags)
        requested_delta = (
            (5.0 + 24.0 * severity)
            * (0.70 + 0.30 * float(action_profile["load"]))
            * peak_gate
            * phase_intensity
        )
        adjustments.append(
            {
                "body_part": part,
                "matched_tags": matched_tags,
                "action_category": action,
                "peak_load": float(peak_load),
                "peak_gate": float(peak_gate),
                "severity": float(severity),
                "requested_adjustment": float(requested_delta),
            }
        )
    return adjustments


def _load_risk_score(
    clip: dict[str, Any],
    profile: dict[str, Any],
    phase: str,
    features: dict[str, Any],
) -> dict[str, Any]:
    action = str(clip["action_category"])
    action_profile = ACTION_PROFILES[action]
    phase_intensity = PHASE_LOAD_MULTIPLIER[phase]
    peak_load = float(features["summary"]["peak_load"])
    mechanical = min(100.0, float(action_profile["load"]) * phase_intensity * 100.0)
    volume = 100.0 * clamp(float(clip["repetition_count"]) / 20.0)
    duration = 100.0 * clamp(float(clip["duration_sec"]) / 15.0)

    route_features = [
        _feature("peak_load", "峰值三维负荷", peak_load, 0.50),
        _feature("mechanical_demand", "动作类别机械负荷", mechanical, 0.20),
        _feature("repetition_volume", "重复次数负荷", volume, 0.18),
        _feature("duration_exposure", "持续时间暴露", duration, 0.12),
    ]
    risk = sum(item["contribution"] for item in route_features)
    base_risk = risk
    injury_adjustments = _injury_adjustments(clip, profile, phase, peak_load)
    applied_adjustment = 0.0
    for adjustment in injury_adjustments:
        before = risk
        after = min(100.0, risk + adjustment["requested_adjustment"])
        applied = after - before
        risk = after
        applied_adjustment += applied
        adjustment["before_risk"] = float(before)
        adjustment["after_risk"] = float(after)
        adjustment["adjustment_delta"] = float(applied)
        adjustment["uplift_pct"] = float(
            applied / base_risk * 100.0 if base_risk > 1e-12 else applied
        )
        route_features.append(
            {
                "key": f"injury_risk_{adjustment['body_part']}",
                "label": f"伤病标签负荷上调（{adjustment['body_part']}）",
                "component": float(applied),
                "weight": 1.0,
                "contribution": float(applied),
                "kind": "injury_adjustment",
            }
        )

    return {
        "score": float(risk),
        "base_score": float(base_risk),
        "injury_adjustment": float(applied_adjustment),
        "features": route_features,
        "injury_adjustments": [
            adjustment
            for adjustment in injury_adjustments
            if adjustment["adjustment_delta"] > 0.0
        ],
    }


def _profile_match_score(
    clip: dict[str, Any],
    profile: dict[str, Any],
    phase: str,
    features: dict[str, Any],
) -> dict[str, Any]:
    action = str(clip["action_category"])
    specialty = str(profile["specialty"])
    training_years = float(profile["training_years"])
    action_profile = ACTION_PROFILES[action]
    affinity = float(
        SPECIALTY_ACTION_AFFINITY.get(specialty, {}).get(action, 0.36)
    )
    demand_years = float(action_profile["demand_years"])
    experience_fit = 100.0 * clamp(
        1.0 - abs(training_years - demand_years) / max(12.0, demand_years + 5.0)
    )

    injured_parts = injury_body_parts(profile.get("injury_tags", []))
    if injured_parts:
        stressed = set(injured_parts) & set(action_profile["body_parts"])
        injury_fit = 100.0 * (
            1.0 - 0.55 * len(stressed) / max(1, len(injured_parts))
        )
    else:
        injury_fit = 100.0

    target_load = PHASE_TARGET_LOAD[phase]
    phase_fit = 100.0 * clamp(
        1.0 - abs(float(action_profile["load"]) - target_load) / 0.55
    )
    route_features = [
        _feature("specialty_affinity", "专项—动作亲和度", affinity * 100.0, 0.40),
        _feature("experience_fit", "训练年限匹配度", experience_fit, 0.24),
        _feature("injury_compatibility", "旧伤动作相容度", injury_fit, 0.22),
        _feature("phase_fit", "周期阶段适配度", phase_fit, 0.14),
    ]
    return {
        "score": sum(item["contribution"] for item in route_features),
        "features": route_features,
        "alignment": features["alignment"],
    }


def score_candidate(
    clip: dict[str, Any],
    profile: dict[str, Any],
    phase: str,
    weights: dict[str, float] | None = None,
    *,
    features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_weights = validate_weights(weights or DEFAULT_PHASE_WEIGHTS[phase])
    feature_payload = features or extract_features(clip, profile, phase)
    quality = _quality_score(clip, feature_payload)
    load_risk = _load_risk_score(clip, profile, phase, feature_payload)
    profile_match = _profile_match_score(clip, profile, phase, feature_payload)

    risk_safety = 100.0 - load_risk["score"]
    fused_contributions = {
        "quality": normalized_weights["quality"] * quality["score"],
        "risk": normalized_weights["risk"] * risk_safety,
        "profile": normalized_weights["profile"] * profile_match["score"],
    }
    fused_score = sum(fused_contributions.values())
    return {
        "clip_id": clip["clip_id"],
        "athlete_id": clip["athlete_id"],
        "action_category": clip["action_category"],
        "repetition_count": int(clip["repetition_count"]),
        "duration_sec": float(clip["duration_sec"]),
        "manual_quality_score": float(clip["manual_quality_score"]),
        "metadata": dict(clip.get("metadata", {})),
        "statistics": feature_payload["channels"],
        "statistics_summary": feature_payload["summary"],
        "alignment": profile_match["alignment"],
        "scores": {
            "quality": float(quality["score"]),
            "load_risk": float(load_risk["score"]),
            "risk_safety": float(risk_safety),
            "profile_match": float(profile_match["score"]),
            "fused": float(fused_score),
        },
        "score_breakdown": {
            "weights": normalized_weights,
            "quality": {
                "score": float(quality["score"]),
                "contribution": float(fused_contributions["quality"]),
                "features": quality["features"],
            },
            "load_risk": {
                "score": float(load_risk["score"]),
                "base_score": float(load_risk["base_score"]),
                "injury_adjustment": float(load_risk["injury_adjustment"]),
                "risk_safety": float(risk_safety),
                "contribution": float(fused_contributions["risk"]),
                "features": load_risk["features"],
                "injury_adjustments": load_risk["injury_adjustments"],
            },
            "profile_match": {
                "score": float(profile_match["score"]),
                "contribution": float(fused_contributions["profile"]),
                "features": profile_match["features"],
            },
            "fused_contributions": fused_contributions,
            "fused_score": float(fused_score),
        },
        "injury_risk_uplift": load_risk["injury_adjustments"],
        "raw_array_lengths": feature_payload["sample_counts"],
    }


def score_signature(candidate: dict[str, Any]) -> tuple[float, float, float, float]:
    scores = candidate["scores"]
    return (
        float(scores["quality"]),
        float(scores["load_risk"]),
        float(scores["profile_match"]),
        float(scores["fused"]),
    )
