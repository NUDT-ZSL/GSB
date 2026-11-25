from __future__ import annotations

from typing import Any

from .clock import check_clock

SCORE_SCALE = 10_000


def _bounded_int(value: Any, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def evaluate_heat(
    obj: dict[str, Any],
    now_clock_ms: int,
    heat_config: dict[str, Any],
    *,
    clock_watermark_ms: int | None = None,
    clock_tolerance_ms: int = 0,
) -> dict[str, Any]:
    """Return an exact integer heat score.

    Components are combined in the declared order: recency, access count,
    then declared heat weight.  All divisions use floor and all outputs are
    plain sorted-JSON-safe integers/strings.
    """

    if clock_watermark_ms is not None:
        check_clock(now_clock_ms, clock_watermark_ms, clock_tolerance_ms)

    last_access = int(obj["last_access_clock_ms"])
    age_ms = max(0, now_clock_ms - last_access)
    window_ms = int(heat_config.get("recency_window_ms", 600_000))
    recency_raw = max(0, SCORE_SCALE - (age_ms * SCORE_SCALE) // window_ms)
    recency_score = _bounded_int(recency_raw, 0, SCORE_SCALE)

    frequency_unit = int(heat_config.get("frequency_unit_bp", 200))
    frequency_score = _bounded_int(int(obj["access_count"]) * frequency_unit, 0, SCORE_SCALE)

    weight = _bounded_int(int(obj["heat_weight"]), 0, 100)
    weight_score = weight * 100

    recency_weight = int(heat_config.get("recency_weight", 60))
    frequency_weight = int(heat_config.get("frequency_weight", 25))
    object_weight = int(heat_config.get("weight_weight", 15))
    weight_sum = recency_weight + frequency_weight + object_weight
    numerator = (
        recency_score * recency_weight
        + frequency_score * frequency_weight
        + weight_score * object_weight
    )
    score = numerator // weight_sum

    hot_threshold = int(heat_config["hot_threshold"])
    cold_threshold = int(heat_config["cold_threshold"])
    declared_tier = obj.get("current_tier") or obj["declared_tier"]
    if score >= hot_threshold:
        target_tier = "hot"
        decision = "hot_threshold"
    elif score <= cold_threshold:
        target_tier = "cold"
        decision = "cold_threshold"
    else:
        target_tier = declared_tier
        decision = "hysteresis_retain_declared"

    return {
        "object_key": obj["key"],
        "now_clock_ms": now_clock_ms,
        "last_access_clock_ms": last_access,
        "age_ms": age_ms,
        "components": {
            "recency_score_bp": recency_score,
            "access_count": int(obj["access_count"]),
            "frequency_score_bp": frequency_score,
            "heat_weight": weight,
            "weight_score_bp": weight_score,
        },
        "combination_order": ["recency", "access_count", "heat_weight"],
        "weight_sum": weight_sum,
        "score_numerator": numerator,
        "heat_score_bp": score,
        "thresholds_bp": {
            "hot": hot_threshold,
            "cold": cold_threshold,
        },
        "declared_tier": obj["declared_tier"],
        "current_tier": declared_tier,
        "target_tier": target_tier,
        "decision": decision,
        "migration_required": target_tier != declared_tier,
    }


def rank_candidates(judgements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach a deterministic urgency and return candidate judgements."""

    ranked: list[dict[str, Any]] = []
    for item in judgements:
        thresholds = item["thresholds_bp"]
        score = item["heat_score_bp"]
        if item["target_tier"] == "hot" and item["current_tier"] == "cold":
            urgency = score - int(thresholds["hot"]) + 1
            direction = "cold_to_hot"
        elif item["target_tier"] == "cold" and item["current_tier"] == "hot":
            urgency = int(thresholds["cold"]) - score + 1
            direction = "hot_to_cold"
        else:
            urgency = 0
            direction = "none"
        enriched = dict(item)
        enriched["urgency"] = max(1, urgency) if direction != "none" else 0
        enriched["direction"] = direction
        ranked.append(enriched)
    return ranked
