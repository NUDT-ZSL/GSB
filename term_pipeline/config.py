"""Configuration loading and validation."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .errors import E_TERM_ENTRY_INVALID, E_WEIGHT_NEGATIVE, PipelineError
from .util import read_json


DEFAULT_CONFIG: dict[str, Any] = {
    "config_version": "cfg-v1",
    "normalization": "NFC",
    "scoring": {
        "length_weights": {"S": 0.25, "M": 0.75, "L": 1.25},
        "source_weight_scale": 1.0,
        "coverage_weight": 1.0,
        "context_window_chars": 8,
        "round_digits": 12,
    },
    "adaptive": {
        "exact_max_shards": 6,
        "exact_max_candidates_per_doc": 24,
        "exact_max_avg_features": 12,
        "exact_min_memory_headroom_mb": 64.0,
        "resident_memory_headroom_mb": 128.0,
        "approx_max_candidates_per_doc": 16,
        "approx_max_features_per_candidate": 8,
    },
    "budget": {
        "total_compare_budget": 500000,
        "max_resident_cache_mb": 128.0,
        "candidate_compare_cost": 2,
        "feature_compare_cost": 1,
        "bytes_per_candidate": 384,
        "bytes_per_feature": 96,
        "fixed_doc_bytes": 2048,
        "policy": "strict",
    },
    "disambiguation": {
        "tie_break": "sense_id_asc",
        "comparison_recall_floor": 1.0,
    },
    "paths": {
        "shards": ["data/shards/shard-000.jsonl"],
        "glossary": "data/glossary.json",
        "context_features": "data/context_features.json",
        "rules_dir": "data/rules",
        "snapshot_dir": "snapshots",
        "out_dir": "out",
    },
}


def _merge(default: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(default)
    for key, value in supplied.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PipelineError(
            E_TERM_ENTRY_INVALID,
            f"{label} must be an object",
            {"field": label},
        )
    return value


def load_config(path_or_dir: str | Path) -> dict[str, Any]:
    path = Path(path_or_dir)
    if path.is_dir():
        path = path / "config.json"
    path = path.resolve()
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise PipelineError(
            E_TERM_ENTRY_INVALID,
            "config must be a JSON object",
            {"config": str(path)},
        )
    config = _merge(DEFAULT_CONFIG, raw)
    config["_config_path"] = str(path)
    config["_root"] = str(path.parent)

    for section in ("scoring", "adaptive", "budget", "disambiguation", "paths"):
        _require_mapping(config.get(section), section)

    if config["normalization"] not in {"NFC", "NFKC", "NONE"}:
        raise PipelineError(
            E_TERM_ENTRY_INVALID,
            "unsupported normalization mode",
            {"normalization": config["normalization"]},
        )

    scoring = config["scoring"]
    digits = scoring.get("round_digits")
    if not isinstance(digits, int) or isinstance(digits, bool) or digits < 0:
        raise PipelineError(
            E_TERM_ENTRY_INVALID,
            "scoring.round_digits must be a non-negative integer",
            {"round_digits": digits},
        )
    for name in ("source_weight_scale", "coverage_weight"):
        value = scoring.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise PipelineError(
                E_TERM_ENTRY_INVALID,
                f"scoring.{name} must be numeric",
                {"field": name, "value": value},
            )
        if value < 0:
            raise PipelineError(
                E_WEIGHT_NEGATIVE,
                f"scoring.{name} must not be negative",
                {"field": name, "value": value},
            )
    window_chars = scoring.get("context_window_chars")
    if (
        not isinstance(window_chars, (int, float))
        or isinstance(window_chars, bool)
        or window_chars < 0
    ):
        raise PipelineError(
            E_TERM_ENTRY_INVALID,
            "scoring.context_window_chars must be non-negative",
            {"field": "context_window_chars", "value": window_chars},
        )
    length_weights = scoring.get("length_weights")
    if not isinstance(length_weights, dict) or not length_weights:
        raise PipelineError(
            E_TERM_ENTRY_INVALID,
            "scoring.length_weights must be a non-empty object",
            {"length_weights": length_weights},
        )
    for tier, value in length_weights.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise PipelineError(
                E_TERM_ENTRY_INVALID,
                "length weight must be numeric",
                {"tier": tier, "value": value},
            )
        if value < 0:
            raise PipelineError(
                E_WEIGHT_NEGATIVE,
                "length weight must not be negative",
                {"tier": tier, "value": value},
            )

    budget = config["budget"]
    if budget.get("policy") not in {"strict", "report"}:
        raise PipelineError(
            E_TERM_ENTRY_INVALID,
            "budget.policy must be strict or report",
            {"policy": budget.get("policy")},
        )
    for key in (
        "total_compare_budget",
        "max_resident_cache_mb",
        "candidate_compare_cost",
        "feature_compare_cost",
        "bytes_per_candidate",
        "bytes_per_feature",
        "fixed_doc_bytes",
    ):
        value = budget.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise PipelineError(
                E_TERM_ENTRY_INVALID,
                f"budget.{key} must be non-negative",
                {"field": key, "value": value},
            )

    paths = config["paths"]
    shards = paths.get("shards")
    if not isinstance(shards, list) or not shards:
        raise PipelineError(
            E_TERM_ENTRY_INVALID,
            "paths.shards must be a non-empty list",
            {"shards": shards},
        )

    base = path.parent
    resolved = {}
    for key in ("glossary", "context_features", "rules_dir", "snapshot_dir", "out_dir"):
        value = paths.get(key)
        if not isinstance(value, str) or not value:
            raise PipelineError(
                E_TERM_ENTRY_INVALID,
                f"paths.{key} must be a non-empty string",
                {"field": key, "value": value},
            )
        resolved[key] = str((base / value).resolve())
    resolved["shards"] = [str((base / value).resolve()) for value in shards]
    config["_resolved_paths"] = resolved
    return config


def input_file_hashes(config: dict[str, Any]) -> dict[str, str]:
    from .util import sha256_bytes

    hashes: dict[str, str] = {}
    paths = config["_resolved_paths"]
    for path_text in paths["shards"]:
        path = Path(path_text)
        hashes[f"shard:{path.name}"] = sha256_bytes(path.read_bytes())
    for key in ("glossary", "context_features"):
        path = Path(paths[key])
        hashes[key] = sha256_bytes(path.read_bytes())
    return hashes

