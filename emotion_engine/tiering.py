"""Deterministic threshold mapping and tie rules."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .config import EngineConfig
from .util import sanitize_float


def _strict_valence_band(value: float, thresholds: Sequence[float]) -> str:
    if value < thresholds[0]:
        return "negative"
    if value < thresholds[1]:
        return "neutral"
    return "positive"


def _strict_arousal_band(value: float, threshold: float) -> str:
    return "high" if value >= threshold else "low"


def _near(value: float, threshold: float, tolerance: float) -> bool:
    return abs(value - threshold) <= tolerance


def classify_bands(
    valence: float,
    arousal: float,
    previous_valence: str | None,
    previous_arousal: str | None,
    config: EngineConfig,
) -> dict[str, Any]:
    """Classify one frame, optionally retaining a band inside a tolerance dead-band."""

    guard = config.subnormal_guard
    valence = sanitize_float(float(valence), guard)
    arousal = sanitize_float(float(arousal), guard)
    thresholds = [float(item) for item in config.tier_schema["valence_thresholds"]]
    arousal_threshold = float(config.tier_schema["arousal_threshold"])
    strategy = config.strategy
    tolerance = config.tolerance

    strict_valence = _strict_valence_band(valence, thresholds)
    strict_arousal = _strict_arousal_band(arousal, arousal_threshold)
    chosen_valence = strict_valence
    chosen_arousal = strict_arousal
    near_axes: list[dict[str, Any]] = []
    stays: list[dict[str, Any]] = []

    if _near(valence, thresholds[0], tolerance) or _near(valence, thresholds[1], tolerance):
        nearest = min(thresholds, key=lambda item: (abs(valence - item), item))
        near_axes.append(
            {
                "axis": "valence",
                "value": valence,
                "threshold": nearest,
                "distance": abs(valence - nearest),
            }
        )
        if strategy == "tolerance" and previous_valence is not None:
            chosen_valence = previous_valence
            if chosen_valence != strict_valence:
                stays.append(
                    {
                        "axis": "valence",
                        "value": valence,
                        "strict_band": strict_valence,
                        "retained_band": chosen_valence,
                    }
                )

    if _near(arousal, arousal_threshold, tolerance):
        near_axes.append(
            {
                "axis": "arousal",
                "value": arousal,
                "threshold": arousal_threshold,
                "distance": abs(arousal - arousal_threshold),
            }
        )
        if strategy == "tolerance" and previous_arousal is not None:
            chosen_arousal = previous_arousal
            if chosen_arousal != strict_arousal:
                stays.append(
                    {
                        "axis": "arousal",
                        "value": arousal,
                        "strict_band": strict_arousal,
                        "retained_band": chosen_arousal,
                    }
                )

    tier_map = {
        (str(item["valence_band"]), str(item["arousal_band"])): str(item["tier"])
        for item in config.tier_schema["tier_map"]
    }
    tier = tier_map[(chosen_valence, chosen_arousal)]
    return {
        "valence_band": chosen_valence,
        "arousal_band": chosen_arousal,
        "strict_valence_band": strict_valence,
        "strict_arousal_band": strict_arousal,
        "tier": tier,
        "near_axes": near_axes,
        "stays": stays,
    }


def classify_sequence(frames: Sequence[Mapping[str, Any]], config: EngineConfig) -> dict[str, Any]:
    """Classify frames in index order using explicit hysteresis state."""

    previous_valence: str | None = None
    previous_arousal: str | None = None
    tiers: list[str] = []
    near_frames: list[dict[str, Any]] = []
    stays: list[dict[str, Any]] = []
    for expected_index, frame in enumerate(frames):
        frame_index = int(frame.get("frame_index", expected_index))
        result = classify_bands(
            float(frame["valence"]),
            float(frame["arousal"]),
            previous_valence,
            previous_arousal,
            config,
        )
        tiers.append(result["tier"])
        if result["near_axes"]:
            near_frames.append(
                {
                    "frame_index": frame_index,
                    "axes": result["near_axes"],
                }
            )
        for stay in result["stays"]:
            stays.append({"frame_index": frame_index, **stay})
        previous_valence = result["valence_band"]
        previous_arousal = result["arousal_band"]

    switches = sum(1 for left, right in zip(tiers, tiers[1:]) if left != right)
    return {
        "tiers": tiers,
        "near_threshold_frames": near_frames,
        "threshold_stays": stays,
        "threshold_stay_count": len(stays),
        "instant_tier_switch_count": switches,
    }


def tier_order_map(config: EngineConfig) -> dict[str, int]:
    return {
        str(label): index
        for index, label in enumerate(config.tier_schema["tier_labels"])
    }


def tier_counts(tiers: Iterable[str], config: EngineConfig) -> dict[str, int]:
    counts = {str(label): 0 for label in config.tier_schema["tier_labels"]}
    for tier in tiers:
        counts[str(tier)] += 1
    return counts


def dominant_tier(counts: Mapping[str, int], config: EngineConfig) -> str:
    """Choose the highest count; ties use tier_schema order, not dict order."""

    order = tier_order_map(config)
    labels = [str(label) for label in config.tier_schema["tier_labels"]]
    return min(
        labels,
        key=lambda label: (-int(counts.get(label, 0)), order[label]),
    )


def count_tier_switches(tiers: Sequence[str]) -> int:
    return sum(1 for left, right in zip(tiers, tiers[1:]) if left != right)
