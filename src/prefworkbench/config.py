from __future__ import annotations

import copy
from typing import Any

from .errors import E_CONFIG, WorkbenchError


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "quality_clean": {
        "weights": {"length": 0.40, "repetition": 0.35, "noise": 0.25},
        "ideal_length_chars": 120,
        "min_score": 0.50,
        "flag_penalty": {"ok": 0.0, "warn": 0.08, "bad": 0.30},
        "blocked_flags": ["bad"],
        "tier_thresholds": {"high": 0.74, "medium": 0.55},
        "ngram_size": 3,
        "token_pattern": r"[^\W_]+",
        "allowed_punctuation": ".,!?;:'\"()[]{}<>/\\-_=+*#@%&$|~`",
    },
    "near_duplicate": {
        "token_pattern": r"[^\W_]+",
        "signature_size": 16,
        "similarity_threshold": 0.72,
        "seed": 7001,
        "max_comparisons": 20000,
        "on_limit": "error",
    },
    "pair_consistency": {
        "token_pattern": r"[^\W_]+",
        "prompt_similarity_threshold": 0.82,
        "conflict_policy": "error",
    },
    "sampling": {
        "seed": 31007,
        "total_target": 24,
        "per_annotator_group": {"alpha": 9, "beta": 8, "gamma": 7},
        "per_quality_tier": {"high": 12, "medium": 8, "low": 4},
    },
    "resources": {
        "max_manifest_bytes": 4_000_000,
        "max_parallel_nodes": 2,
        "on_limit": "error",
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _require_number(value: Any, location: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkbenchError(E_CONFIG, f"{location} must be numeric", {"location": location})
    number = float(value)
    if minimum is not None and number < minimum:
        raise WorkbenchError(
            E_CONFIG,
            f"{location} must be >= {minimum}",
            {"location": location, "value": value},
        )
    return number


def _require_int(value: Any, location: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkbenchError(E_CONFIG, f"{location} must be an integer", {"location": location})
    if value < minimum:
        raise WorkbenchError(
            E_CONFIG,
            f"{location} must be >= {minimum}",
            {"location": location, "value": value},
        )
    return value


def validate_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    if raw is not None and not isinstance(raw, dict):
        raise WorkbenchError(E_CONFIG, "configuration root must be an object")
    config = _deep_merge(DEFAULT_CONFIG, raw or {})

    quality = config["quality_clean"]
    weights = quality["weights"]
    if not isinstance(weights, dict) or set(weights) != {"length", "repetition", "noise"}:
        raise WorkbenchError(
            E_CONFIG,
            "quality_clean.weights must contain length, repetition and noise",
        )
    for name, value in weights.items():
        _require_number(value, f"quality_clean.weights.{name}", minimum=0.0)
    if sum(float(value) for value in weights.values()) <= 0:
        raise WorkbenchError(E_CONFIG, "quality_clean.weights cannot all be zero")

    _require_number(quality["ideal_length_chars"], "quality_clean.ideal_length_chars", minimum=1.0)
    _require_number(quality["min_score"], "quality_clean.min_score", minimum=0.0)
    if _require_number(quality["min_score"], "quality_clean.min_score") > 1.0:
        raise WorkbenchError(E_CONFIG, "quality_clean.min_score must be <= 1")
    _require_int(quality["ngram_size"], "quality_clean.ngram_size", minimum=1)
    if not isinstance(quality["token_pattern"], str) or not quality["token_pattern"]:
        raise WorkbenchError(E_CONFIG, "quality_clean.token_pattern must be a non-empty string")
    if not isinstance(quality["allowed_punctuation"], str):
        raise WorkbenchError(E_CONFIG, "quality_clean.allowed_punctuation must be a string")

    penalties = quality["flag_penalty"]
    if not isinstance(penalties, dict):
        raise WorkbenchError(E_CONFIG, "quality_clean.flag_penalty must be an object")
    for name, value in penalties.items():
        _require_number(value, f"quality_clean.flag_penalty.{name}", minimum=0.0)
    blocked = quality["blocked_flags"]
    if not isinstance(blocked, list) or not all(isinstance(item, str) for item in blocked):
        raise WorkbenchError(E_CONFIG, "quality_clean.blocked_flags must be a string list")

    thresholds = quality["tier_thresholds"]
    if not isinstance(thresholds, dict) or set(thresholds) != {"high", "medium"}:
        raise WorkbenchError(
            E_CONFIG,
            "quality_clean.tier_thresholds must contain high and medium",
        )
    high = _require_number(thresholds["high"], "quality_clean.tier_thresholds.high")
    medium = _require_number(thresholds["medium"], "quality_clean.tier_thresholds.medium")
    if not 0.0 <= medium <= high <= 1.0:
        raise WorkbenchError(
            E_CONFIG,
            "quality tier thresholds must satisfy 0 <= medium <= high <= 1",
        )

    near = config["near_duplicate"]
    _require_int(near["signature_size"], "near_duplicate.signature_size")
    similarity = _require_number(
        near["similarity_threshold"],
        "near_duplicate.similarity_threshold",
        minimum=0.0,
    )
    if similarity > 1.0:
        raise WorkbenchError(E_CONFIG, "near_duplicate.similarity_threshold must be <= 1")
    _require_int(near["seed"], "near_duplicate.seed", minimum=0)
    _require_int(near["max_comparisons"], "near_duplicate.max_comparisons")
    if near["on_limit"] not in {"error", "degrade"}:
        raise WorkbenchError(E_CONFIG, "near_duplicate.on_limit must be error or degrade")
    if not isinstance(near["token_pattern"], str) or not near["token_pattern"]:
        raise WorkbenchError(E_CONFIG, "near_duplicate.token_pattern must be a non-empty string")

    consistency = config["pair_consistency"]
    if not isinstance(consistency["token_pattern"], str) or not consistency["token_pattern"]:
        raise WorkbenchError(
            E_CONFIG,
            "pair_consistency.token_pattern must be a non-empty string",
        )
    prompt_similarity = _require_number(
        consistency["prompt_similarity_threshold"],
        "pair_consistency.prompt_similarity_threshold",
        minimum=0.0,
    )
    if prompt_similarity > 1.0:
        raise WorkbenchError(
            E_CONFIG,
            "pair_consistency.prompt_similarity_threshold must be <= 1",
        )
    if consistency["conflict_policy"] not in {
        "error",
        "keep_and_flag",
        "drop_conflicting_pairs",
    }:
        raise WorkbenchError(
            E_CONFIG,
            "pair_consistency.conflict_policy must be error, keep_and_flag or drop_conflicting_pairs",
        )

    sampling = config["sampling"]
    _require_int(sampling["seed"], "sampling.seed", minimum=0)
    _require_int(sampling["total_target"], "sampling.total_target", minimum=0)
    for key in ("per_annotator_group", "per_quality_tier"):
        targets = sampling[key]
        if not isinstance(targets, dict):
            raise WorkbenchError(E_CONFIG, f"sampling.{key} must be an object")
        for name, value in targets.items():
            _require_int(value, f"sampling.{key}.{name}", minimum=0)

    resources = config["resources"]
    _require_int(resources["max_manifest_bytes"], "resources.max_manifest_bytes")
    _require_int(resources["max_parallel_nodes"], "resources.max_parallel_nodes")
    if resources["on_limit"] not in {"error", "degrade"}:
        raise WorkbenchError(E_CONFIG, "resources.on_limit must be error or degrade")

    return config


def node_params(config: dict[str, Any], node: str) -> dict[str, Any]:
    """Return exactly the configuration slice that can affect one node."""

    if node == "quality_clean":
        return copy.deepcopy(config["quality_clean"])
    if node == "near_duplicate":
        params = copy.deepcopy(config["near_duplicate"])
        params["max_comparisons"] = int(params["max_comparisons"])
        return params
    if node == "pair_consistency":
        return copy.deepcopy(config["pair_consistency"])
    if node == "allocation_sampling":
        return copy.deepcopy(config["sampling"])
    raise WorkbenchError(E_CONFIG, f"unknown node: {node}")
