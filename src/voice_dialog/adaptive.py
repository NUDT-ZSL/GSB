"""Deterministic scale-aware strategy selection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .constants import BALANCED_FEATURES, COMPACT_FEATURES, CORE_FEATURES
from .util import canonicalize, stable_float


@dataclass(frozen=True)
class StrategyCandidate:
    strategy_id: str
    features: tuple[str, ...]
    classifier: str
    fusion_window: int
    base_score: float
    ideal_segments: int
    ideal_duration: float
    ideal_turns: int


STRATEGY_TABLE: dict[str, tuple[StrategyCandidate, ...]] = {
    "small": (
        StrategyCandidate("small_balanced_nc", BALANCED_FEATURES, "nearest_centroid", 3, 0.92, 32, 0.75, 6),
        StrategyCandidate("small_core_nc", CORE_FEATURES, "nearest_centroid", 2, 0.86, 18, 0.60, 3),
        StrategyCandidate("small_balanced_lr", BALANCED_FEATURES, "weighted_logistic_regression", 4, 0.74, 55, 0.85, 9),
    ),
    "medium": (
        StrategyCandidate("medium_balanced_lr", BALANCED_FEATURES, "weighted_logistic_regression", 5, 0.93, 220, 0.90, 24),
        StrategyCandidate("medium_core_lr", CORE_FEATURES, "weighted_logistic_regression", 3, 0.84, 140, 0.75, 14),
        StrategyCandidate("medium_compact_nc", COMPACT_FEATURES, "nearest_centroid", 2, 0.76, 90, 0.65, 9),
    ),
    "large": (
        StrategyCandidate("large_compact_nc", COMPACT_FEATURES, "nearest_centroid", 2, 0.94, 1600, 0.85, 100),
        StrategyCandidate("large_core_lr", CORE_FEATURES, "weighted_logistic_regression", 3, 0.85, 900, 0.70, 65),
        StrategyCandidate("large_balanced_lr", BALANCED_FEATURES, "weighted_logistic_regression", 5, 0.72, 2600, 1.00, 160),
    ),
}


def scale_metrics(
    *,
    segment_count: int,
    valid_durations: list[float],
    turn_count: int,
    granularity: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    durations = [float(value) for value in valid_durations if math.isfinite(value) and value > 0]
    average_duration = sum(durations) / len(durations) if durations else 0.0
    duration_weight = float(config["scale_tiers"]["duration_weight"])
    volume_units = float(segment_count) + 2.0 * float(turn_count) + duration_weight * average_duration
    if config["scale_tier"] == "auto":
        tiers = config["scale_tiers"]
        if volume_units <= float(tiers["small_max_units"]):
            tier = "small"
        elif volume_units <= float(tiers["medium_max_units"]):
            tier = "medium"
        else:
            tier = "large"
        tier_source = "auto"
    else:
        tier = str(config["scale_tier"])
        tier_source = "config_override"
    return canonicalize(
        {
            "segments": segment_count,
            "valid_segments": len(durations),
            "average_duration_sec": average_duration,
            "turns": turn_count,
            "granularity": granularity,
            "volume_units": volume_units,
            "tier": tier,
            "tier_source": tier_source,
            "thresholds": config["scale_tiers"],
        }
    )


def _fit(value: float, ideal: float) -> float:
    if ideal <= 0:
        return 0.0
    if value <= 0:
        return 0.0
    if ideal == 0.0:
        return 1.0
    closeness = abs(math.log1p(value) - math.log1p(ideal)) / max(math.log1p(ideal), 1.0)
    return max(0.0, 1.0 - min(closeness, 1.0))


def _candidate_record(
    candidate: StrategyCandidate,
    *,
    metrics: dict[str, Any],
    granularity: str,
) -> dict[str, Any]:
    segment_fit = _fit(metrics["segments"], candidate.ideal_segments)
    duration_fit = _fit(metrics["average_duration_sec"], candidate.ideal_duration)
    turn_fit = _fit(metrics["turns"], candidate.ideal_turns)
    granularity_fit = 1.0
    if granularity == "fine" and candidate.classifier == "nearest_centroid":
        granularity_fit = 0.96
    if granularity == "coarse" and candidate.classifier == "weighted_logistic_regression":
        granularity_fit = 0.97
    score = (
        0.40 * candidate.base_score
        + 0.20 * segment_fit
        + 0.15 * duration_fit
        + 0.15 * turn_fit
        + 0.10 * granularity_fit
    )
    return canonicalize(
        {
            "strategy_id": candidate.strategy_id,
            "feature_set": list(candidate.features),
            "feature_count": len(candidate.features),
            "classifier": candidate.classifier,
            "fusion_window": candidate.fusion_window,
            "base_score": candidate.base_score,
            "score_components": {
                "base": candidate.base_score,
                "segment_fit": segment_fit,
                "duration_fit": duration_fit,
                "turn_fit": turn_fit,
                "granularity_fit": granularity_fit,
            },
            "score": stable_float(score, 6),
        }
    )


def select_strategy(
    *,
    segment_count: int,
    valid_durations: list[float],
    turn_count: int,
    granularity: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    metrics = scale_metrics(
        segment_count=segment_count,
        valid_durations=valid_durations,
        turn_count=turn_count,
        granularity=granularity,
        config=config,
    )
    tier = metrics["tier"]
    candidates = [
        _candidate_record(candidate, metrics=metrics, granularity=granularity)
        for candidate in STRATEGY_TABLE[tier]
    ]
    candidates.sort(key=lambda record: (-record["score"], record["strategy_id"]))
    selected = dict(candidates[0])
    selected["selected"] = True
    for candidate in candidates[1:]:
        candidate["selected"] = False

    allowed_features = set(config["acoustic_feature_set"])
    effective_features = [name for name in selected["feature_set"] if name in allowed_features]
    limit = int(config["feature_dimension_limit"])
    truncated = effective_features[:limit]
    if not truncated:
        raise ValueError("adaptive strategy selected no configured features")
    effective_classifier = selected["classifier"]
    if config["classifier"] != "auto":
        effective_classifier = config["classifier"]
    if config["fusion"]["window_mode"] == "fixed":
        effective_window = int(config["fusion"]["window"])
    else:
        effective_window = int(selected["fusion_window"])
    effective_features = truncated
    constraints = {
        "classifier_override": config["classifier"] if config["classifier"] != "auto" else "",
        "fusion_window_override": (
            int(config["fusion"]["window"]) if config["fusion"]["window_mode"] == "fixed" else None
        ),
        "feature_dimension_limit": limit,
        "feature_count_before_limit": len(selected["feature_set"]),
        "feature_count_after_limit": len(effective_features),
        "truncated": len(selected["feature_set"]) > len(effective_features),
    }
    reason = (
        f"tier={tier} from {metrics['tier_source']} scale metrics "
        f"(segments={metrics['segments']}, turns={metrics['turns']}, "
        f"avg_duration={metrics['average_duration_sec']:.6f}, units={metrics['volume_units']:.6f}); "
        f"strategy={selected['strategy_id']} scored {selected['score']:.6f} with "
        f"{len(effective_features)} effective features."
    )
    decision = {
        "metrics": metrics,
        "candidates": candidates,
        "selected": selected,
        "effective": {
            "strategy_id": selected["strategy_id"],
            "feature_names": effective_features,
            "feature_count": len(effective_features),
            "classifier": effective_classifier,
            "fusion_window": effective_window,
        },
        "constraints": constraints,
        "reason": reason,
    }
    return canonicalize(decision)
