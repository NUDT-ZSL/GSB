"""Scale-adaptive recall strategy selector.

The selector is fully deterministic: the same scale metrics always produce the
same decision. Each candidate strategy is scored against observed metrics and
the full scoring trail is returned for auditing.
"""

from __future__ import annotations

from typing import Any

from .parser import ParsedData

STRATEGY_TABLE: list[dict[str, Any]] = [
    {
        "strategy_id": "cold_bootstrap",
        "channels": {"lexical": True, "collaborative": False, "popularity": True},
        "channel_limits": {"lexical": 20, "collaborative": 0, "popularity": 20},
        "fusion_strategy": "rrf",
        "fit": {"cold_ratio_min": 0.25, "density_max": 0.02},
        "rationale": "high cold-start share: collaborative signal is unreliable, rely on lexical + popularity with RRF",
    },
    {
        "strategy_id": "dense_balanced",
        "channels": {"lexical": True, "collaborative": True, "popularity": True},
        "channel_limits": {"lexical": 50, "collaborative": 50, "popularity": 50},
        "fusion_strategy": "weighted",
        "fit": {"density_min": 0.05, "cold_ratio_max": 0.2},
        "rationale": "dense interaction graph: full three-channel weighted fusion with collaborative filtering",
    },
    {
        "strategy_id": "large_sparse",
        "channels": {"lexical": True, "collaborative": True, "popularity": True},
        "channel_limits": {"lexical": 30, "collaborative": 30, "popularity": 20},
        "fusion_strategy": "rrf",
        "fit": {"n_items_min": 45, "density_max": 0.05},
        "rationale": "large sparse catalogue: bounded candidate limits and rank fusion control cost and noise",
    },
    {
        "strategy_id": "small_default",
        "channels": {"lexical": True, "collaborative": True, "popularity": True},
        "channel_limits": {"lexical": 30, "collaborative": 30, "popularity": 30},
        "fusion_strategy": "weighted",
        "fit": {},
        "rationale": "default balanced policy for small and medium catalogues",
    },
]


def compute_metrics(data: ParsedData, declared_users: int) -> dict[str, float]:
    n_users = max(declared_users, data.user_count, 1)
    n_items = max(data.item_count, 1)
    density = data.interaction_count / (n_users * n_items)
    cold_users = max(declared_users - data.user_count, 0)
    long_tail = sum(1 for count in data.item_interaction_counts.values() if count == 1)
    return {
        "n_users": float(declared_users),
        "n_items": float(data.item_count),
        "n_interactions": float(data.interaction_count),
        "density": round(density, 10),
        "cold_ratio": round(cold_users / n_users, 10),
        "long_tail_ratio": round(long_tail / n_items, 10),
    }


def _strategy_score(strategy: dict[str, Any], metrics: dict[str, float]) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    fit = strategy["fit"]
    if "cold_ratio_min" in fit and metrics["cold_ratio"] >= fit["cold_ratio_min"]:
        score += 3.0
        reasons.append(f"cold_ratio {metrics['cold_ratio']} >= {fit['cold_ratio_min']} (+3)")
    if "density_max" in fit and metrics["density"] <= fit["density_max"]:
        score += 2.0
        reasons.append(f"density {metrics['density']} <= {fit['density_max']} (+2)")
    if "density_min" in fit and metrics["density"] >= fit["density_min"]:
        score += 3.0
        reasons.append(f"density {metrics['density']} >= {fit['density_min']} (+3)")
    if "cold_ratio_max" in fit and metrics["cold_ratio"] <= fit["cold_ratio_max"]:
        score += 2.0
        reasons.append(f"cold_ratio {metrics['cold_ratio']} <= {fit['cold_ratio_max']} (+2)")
    if "n_items_min" in fit and metrics["n_items"] >= fit["n_items_min"]:
        score += 3.0
        reasons.append(f"n_items {int(metrics['n_items'])} >= {fit['n_items_min']} (+3)")
    if not fit:
        score += 1.0
        reasons.append("default fallback (+1)")
    return score, reasons


def select_strategy(data: ParsedData, declared_users: int) -> dict[str, Any]:
    metrics = compute_metrics(data, declared_users)
    scoring: list[dict[str, Any]] = []
    for strategy in STRATEGY_TABLE:
        score, reasons = _strategy_score(strategy, metrics)
        scoring.append({
            "strategy_id": strategy["strategy_id"],
            "score": score,
            "matched_conditions": reasons,
        })
    ordered = sorted(scoring, key=lambda row: (-row["score"], row["strategy_id"]))
    winner_id = ordered[0]["strategy_id"]
    winner = next(row for row in STRATEGY_TABLE if row["strategy_id"] == winner_id)
    return {
        "metrics": metrics,
        "candidate_scores": ordered,
        "selected_strategy_id": winner_id,
        "selected": {
            "channels": winner["channels"],
            "channel_limits": winner["channel_limits"],
            "fusion_strategy": winner["fusion_strategy"],
        },
        "reason": winner["rationale"],
    }


def apply_strategy_to_config(config: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """Return a config copy overlaid with the selector decision."""
    overlay = {
        "channels": {
            name: {
                "enabled": decision["selected"]["channels"][name],
                "weight": config["channels"][name]["weight"],
            }
            for name in config["channels"]
        },
        "channel_limits": dict(decision["selected"]["channel_limits"]),
        "fusion": {**config["fusion"], "strategy": decision["selected"]["fusion_strategy"]},
    }
    merged = {**config, **{key: value for key, value in overlay.items()}}
    merged["channels"] = overlay["channels"]
    merged["channel_limits"] = overlay["channel_limits"]
    merged["fusion"] = overlay["fusion"]
    return merged
