"""Recall configuration loading, validation and parameter fingerprints."""

from __future__ import annotations

from typing import Any

from .util import read_json, stable_hash

SEED = "seed"
INTERACTION_TYPES = "interaction_types"
CHANNELS = "channels"
CHANNEL_LIMITS = "channel_limits"
FUSION = "fusion"
FEATURE_WEIGHTS = "feature_weights"
EVALUATION = "evaluation"
RUNTIME = "runtime"

CHANNEL_NAMES = ("lexical", "collaborative", "popularity")
FUSION_STRATEGIES = ("weighted", "rrf")


DEFAULT_CONFIG: dict[str, Any] = {
    SEED: 20261117,
    INTERACTION_TYPES: ["view", "click", "purchase"],
    CHANNELS: {
        "lexical": {"enabled": True, "weight": 0.35},
        "collaborative": {"enabled": True, "weight": 0.40},
        "popularity": {"enabled": True, "weight": 0.25},
    },
    CHANNEL_LIMITS: {"lexical": 50, "collaborative": 50, "popularity": 50},
    FUSION: {
        "strategy": "weighted",
        "rrf_k": 60,
        "redistribution": "proportional",
        "final_list_limit": 30,
        "weighted_mix": 0.5,
    },
    FEATURE_WEIGHTS: {
        "lexical_norm": 1.0,
        "collaborative_norm": 1.0,
        "popularity_norm": 1.0,
        "freshness": 0.2,
    },
    EVALUATION: {
        "sample_ratio": 0.5,
        "cutoffs": [5, 10, 20],
        "holdout_last_n": 1,
    },
    RUNTIME: {
        "max_workers": 4,
        "snapshot_interval": 25,
        "popularity_half_life_days": 7.0,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str) -> dict[str, Any]:
    raw = read_json(path)
    config = _deep_merge(DEFAULT_CONFIG, raw if isinstance(raw, dict) else {})
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    if not isinstance(config[SEED], int):
        raise ValueError("seed must be an integer")
    if not config[INTERACTION_TYPES]:
        raise ValueError("interaction_types must not be empty")
    fusion = config[FUSION]
    if fusion["strategy"] not in FUSION_STRATEGIES:
        raise ValueError(f"unknown fusion strategy: {fusion['strategy']}")
    if fusion["redistribution"] not in ("proportional", "equal"):
        raise ValueError("redistribution must be 'proportional' or 'equal'")
    for name in CHANNEL_NAMES:
        channel = config[CHANNELS][name]
        if not isinstance(channel["enabled"], bool):
            raise ValueError(f"channel {name}.enabled must be boolean")
        if channel["weight"] < 0:
            raise ValueError(f"channel {name}.weight must be >= 0")
        limit = config[CHANNEL_LIMITS][name]
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError(f"channel_limits.{name} must be a positive int")
    for key, value in config[FEATURE_WEIGHTS].items():
        if value < 0:
            raise ValueError(f"feature_weights.{key} must be >= 0")
    eval_cfg = config[EVALUATION]
    if not 0.0 < eval_cfg["sample_ratio"] <= 1.0:
        raise ValueError("evaluation.sample_ratio must be in (0, 1]")
    if config[RUNTIME]["max_workers"] < 1:
        raise ValueError("runtime.max_workers must be >= 1")


def config_hash(config: dict[str, Any]) -> str:
    return stable_hash(config)


def channel_param_fingerprint(config: dict[str, Any], channel: str) -> str:
    """Parameter subset that only affects one recall channel."""
    subset = {
        "seed": config[SEED],
        "enabled": config[CHANNELS][channel]["enabled"],
        "limit": config[CHANNEL_LIMITS][channel],
        "half_life": config[RUNTIME]["popularity_half_life_days"],
    }
    return stable_hash(subset)


def fusion_param_fingerprint(config: dict[str, Any]) -> str:
    return stable_hash(
        {
            "channels": config[CHANNELS],
            "fusion": config[FUSION],
            "feature_weights": config[FEATURE_WEIGHTS],
            "limits": config[CHANNEL_LIMITS],
        }
    )


def evaluation_param_fingerprint(config: dict[str, Any]) -> str:
    return stable_hash({"evaluation": config[EVALUATION], "seed": config[SEED]})
