"""Input loading and strategy configuration resolution."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .errors import BusinessError, E_INVALID_INPUT
from .utils import read_json


DEFAULT_CONFIG: dict[str, Any] = {
    "languages": ["zh", "en"],
    "source_tags": ["wiki", "forum", "manual", "qa"],
    "quality_flags": ["gold", "silver", "bronze"],
    "field_limits": {
        "sample_id": {"min_chars": 1, "max_chars": 128},
        "instruction": {"min_chars": 1, "max_chars": 2048},
        "input": {"min_chars": 0, "max_chars": 8192},
        "output": {"min_chars": 1, "max_chars": 8192},
        "source_tag": {"min_chars": 1, "max_chars": 64},
        "quality_flag": {"min_chars": 1, "max_chars": 32},
        "lang": {"min_chars": 2, "max_chars": 16},
        "annotation_revision": {"min_chars": 1, "max_chars": 64},
    },
    "template": {
        "required_placeholders": {
            "instruction_template": ["instruction"],
            "input_template": ["input"],
        },
        "allowed_placeholders": [
            "instruction",
            "input",
            "output",
            "source_tag",
            "quality_flag",
            "lang",
            "annotation_revision",
            "annotation_revision_raw",
        ],
    },
    "quality": {
        "weights": {"length": 0.35, "repetition": 0.35, "noise": 0.30},
        "length": {
            "min_chars": 40,
            "ideal_min": 120,
            "ideal_max": 900,
            "max_chars": 6000,
        },
        "repetition": {"ngram_size": 3, "zero_at_rate": 0.80},
        "noise": {
            "characters": "\u0000\u0001\u0002\u0007\u0008\u000b\u000c\u001f\ufffd",
            "zero_at_rate": 0.15,
        },
        "tiers": {"gold": 0.82, "silver": 0.65},
    },
    "dedup": {
        "shingle_size": 3,
        "signature_size": 8,
        "band_size": 2,
        "similarity": "minhash_signature_overlap",
        "threshold": 0.75,
        "candidate_windows": [4, 8, 16],
    },
    "sampling": {
        "seed": 20261127,
        "total_target": 18,
        "allow_quality_relaxation": True,
        "max_tier_drop": 1,
        "tier_order": ["gold", "silver", "bronze"],
    },
    "runtime": {
        "requested_workers": 3,
        "max_workers": 4,
        "compare_budget": 5000,
        "memory_limit_bytes": 4 * 1024 * 1024,
        "per_sample_memory_bytes": 4096,
        "per_worker_memory_bytes": 262144,
        "batch_size": 8,
        "pair_batch_size": 64,
        "budget_error_priority": [
            "E_CONCURRENCY_LIMIT",
            "E_COMPARE_BUDGET",
            "E_MEMORY_LIMIT",
        ],
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


DEFAULT_OBJECTIVE_PRIORITY = [
    "progress",
    "memory",
    "sampling_target",
    "dedup_quality",
]


def resolve_config(
    strategy: dict[str, Any],
    cleaning_rules: dict[str, Any],
    quotas: dict[str, Any],
) -> dict[str, Any]:
    config = deep_merge(DEFAULT_CONFIG, strategy.get("config", {}))
    if cleaning_rules:
        config["quality"] = deep_merge(
            config["quality"], cleaning_rules
        )
    if quotas:
        sampling = dict(quotas)
        targets = sampling.pop("targets", [])
        config["sampling"] = deep_merge(config["sampling"], sampling)
        config["sampling"]["targets"] = targets
    config.setdefault("priorities", list(strategy.get("priorities", [])))
    return config


def load_data_inputs(data_dir: str | Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    root = Path(data_dir)
    required = {
        "templates": root / "templates.json",
        "cleaning_rules": root / "cleaning_rules.json",
        "quota_targets": root / "quota_targets.json",
    }
    missing = [str(path) for path in required.values() if not path.exists()]
    if missing:
        raise BusinessError(
            E_INVALID_INPUT,
            "data directory is missing required input files",
            details={"missing": missing},
        )
    return (
        read_json(required["templates"]),
        read_json(required["cleaning_rules"]),
        read_json(required["quota_targets"]),
    )

