"""Deterministic configuration loading and field-level validation."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

from .errors import E_CONFIG_INVALID, WorkbenchError


ALLOWED_LANGS = ("zh", "en", "ja", "code", "multi")
VALIDATION_MODES = ("full", "digest")

DEFAULT_LIMITS: dict[str, Any] = {
    "max_shards": 16,
    "max_blocks_per_doc": 96,
    "max_cache_memory_bytes": 8 * 1024 * 1024,
    "latency_budget_ms": 25.0,
    "query_loop_iterations": 41,
    "batch_size": 8,
    "cache_overflow_policy": "evict_then_spill",
}

DEFAULT_LATENCY_MODEL: dict[str, float] = {
    "base_ms": 0.020,
    "per_block_ms": 0.0012,
    "per_comparison_ms": 0.0040,
}

LENGTH_ALIASES = (
    "chunk_signature_length",
    "signature_chunk_length",
    "signature_length",
    "chunk_chars",
    "chunk_length",
)
WINDOW_ALIASES = ("candidate_window", "window_size", "comparison_window")
THRESHOLD_ALIASES = ("similarity_threshold", "threshold")
MODE_ALIASES = ("validation_mode", "check_mode", "mode")
SEED_ALIASES = ("sampling_seed", "seed")
VERSION_ALIASES = ("config_version", "version")


def _is_real_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _pick(data: dict[str, Any], names: Iterable[str], *, required: bool = True) -> tuple[str, Any] | tuple[None, None]:
    found: list[tuple[str, Any]] = [(name, data[name]) for name in names if name in data]
    if not found:
        if required:
            raise WorkbenchError(E_CONFIG_INVALID, "Missing required configuration field", field=names[0])
        return None, None
    values = {value for _, value in found}
    if len(values) > 1:
        raise WorkbenchError(
            E_CONFIG_INVALID,
            "Conflicting aliases for the same configuration parameter",
            field=found[0][0],
            details={"aliases": [name for name, _ in found]},
        )
    return found[0]


def parse_config(raw: dict[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(raw, (str, Path)):
        path = Path(raw)
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkbenchError(E_CONFIG_INVALID, f"Cannot read config JSON: {exc}", field="config_file") from exc
        raw = loaded

    if not isinstance(raw, dict):
        raise WorkbenchError(E_CONFIG_INVALID, "Configuration must be a JSON object", field="$")

    version_name, version = _pick(raw, VERSION_ALIASES, required=False)
    if version is None:
        config_version = 1
    else:
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise WorkbenchError(E_CONFIG_INVALID, "config_version must be a positive integer", field=version_name or "config_version")
        config_version = version

    length_name, length = _pick(raw, LENGTH_ALIASES)
    if not isinstance(length, int) or isinstance(length, bool) or not 1 <= length <= 100_000:
        raise WorkbenchError(E_CONFIG_INVALID, "Chunk signature length must be an integer in [1, 100000]", field=length_name)

    threshold_name, threshold = _pick(raw, THRESHOLD_ALIASES)
    if not _is_real_number(threshold) or not 0.0 <= float(threshold) <= 1.0:
        raise WorkbenchError(E_CONFIG_INVALID, "Similarity threshold must be a finite number in [0, 1]", field=threshold_name)

    window_name, window = _pick(raw, WINDOW_ALIASES)
    if not isinstance(window, int) or isinstance(window, bool) or not 1 <= window <= 100_000:
        raise WorkbenchError(E_CONFIG_INVALID, "Candidate window must be a positive integer", field=window_name)

    mode_name, mode = _pick(raw, MODE_ALIASES)
    if mode not in VALIDATION_MODES:
        raise WorkbenchError(
            E_CONFIG_INVALID,
            f"validation_mode must be one of {VALIDATION_MODES}",
            field=mode_name,
            details={"allowed": list(VALIDATION_MODES)},
        )

    seed_name, seed = _pick(raw, SEED_ALIASES, required=False)
    if seed is None:
        seed = 20261126
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 2**32 - 1:
        raise WorkbenchError(E_CONFIG_INVALID, "sampling_seed must be an integer in [0, 2^32-1]", field=seed_name or "sampling_seed")

    limits = dict(DEFAULT_LIMITS)
    raw_limits = raw.get("limits", {})
    if raw_limits is not None:
        if not isinstance(raw_limits, dict):
            raise WorkbenchError(E_CONFIG_INVALID, "limits must be an object", field="limits")
        limits.update(raw_limits)

    positive_int_fields = {
        "max_shards": (1, 1_000_000),
        "max_blocks_per_doc": (1, 10_000_000),
        "max_cache_memory_bytes": (1024, 10**12),
        "query_loop_iterations": (1, 10_000),
        "batch_size": (1, 100_000),
    }
    for field, (lower, upper) in positive_int_fields.items():
        value = limits.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or not lower <= value <= upper:
            raise WorkbenchError(
                E_CONFIG_INVALID,
                f"limits.{field} must be an integer in [{lower}, {upper}]",
                field=f"limits.{field}",
            )

    budget = limits.get("latency_budget_ms")
    if not _is_real_number(budget) or float(budget) <= 0:
        raise WorkbenchError(
            E_CONFIG_INVALID,
            "limits.latency_budget_ms must be a positive finite number",
            field="limits.latency_budget_ms",
        )
    limits["latency_budget_ms"] = float(budget)

    policy = limits.get("cache_overflow_policy")
    if policy not in ("evict_then_spill", "error"):
        raise WorkbenchError(
            E_CONFIG_INVALID,
            "limits.cache_overflow_policy must be evict_then_spill or error",
            field="limits.cache_overflow_policy",
        )

    latency_model = dict(DEFAULT_LATENCY_MODEL)
    raw_model = raw.get("latency_model", {})
    if raw_model is not None:
        if not isinstance(raw_model, dict):
            raise WorkbenchError(E_CONFIG_INVALID, "latency_model must be an object", field="latency_model")
        latency_model.update(raw_model)
    for field in DEFAULT_LATENCY_MODEL:
        value = latency_model.get(field)
        if not _is_real_number(value) or float(value) < 0:
            raise WorkbenchError(E_CONFIG_INVALID, f"latency_model.{field} must be non-negative", field=f"latency_model.{field}")
        latency_model[field] = float(value)

    allowed_langs = tuple(raw.get("allowed_langs", ALLOWED_LANGS))
    if not isinstance(allowed_langs, (list, tuple)) or not all(isinstance(item, str) and item for item in allowed_langs):
        raise WorkbenchError(E_CONFIG_INVALID, "allowed_langs must be a list of non-empty strings", field="allowed_langs")

    bucket_strategy = raw.get("bucket_strategy", "anchor_digest")
    if bucket_strategy not in ("anchor_digest", "block_digest", "doc_digest"):
        raise WorkbenchError(
            E_CONFIG_INVALID,
            "bucket_strategy must be anchor_digest, block_digest, or doc_digest",
            field="bucket_strategy",
        )
    bucket_anchor_count = raw.get("bucket_anchor_count", 3)
    if not isinstance(bucket_anchor_count, int) or isinstance(bucket_anchor_count, bool) or not 1 <= bucket_anchor_count <= 32:
        raise WorkbenchError(
            E_CONFIG_INVALID,
            "bucket_anchor_count must be an integer in [1, 32]",
            field="bucket_anchor_count",
        )
    bucket_prefix_chars = raw.get("bucket_prefix_chars", 16)
    if not isinstance(bucket_prefix_chars, int) or isinstance(bucket_prefix_chars, bool) or not 4 <= bucket_prefix_chars <= 64:
        raise WorkbenchError(E_CONFIG_INVALID, "bucket_prefix_chars must be an integer in [4, 64]", field="bucket_prefix_chars")

    stage_versions = raw.get("stage_versions", {})
    if not isinstance(stage_versions, dict):
        raise WorkbenchError(E_CONFIG_INVALID, "stage_versions must be an object", field="stage_versions")
    for key, value in stage_versions.items():
        if not isinstance(key, str) or not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise WorkbenchError(E_CONFIG_INVALID, "stage_versions values must be positive integers", field=f"stage_versions.{key}")

    extras = {
        key: value
        for key, value in raw.items()
        if key
        not in {
            *VERSION_ALIASES,
            *LENGTH_ALIASES,
            *THRESHOLD_ALIASES,
            *WINDOW_ALIASES,
            *MODE_ALIASES,
            *SEED_ALIASES,
            "limits",
            "latency_model",
            "allowed_langs",
            "bucket_strategy",
            "bucket_anchor_count",
            "bucket_prefix_chars",
            "stage_versions",
        }
    }

    return {
        "config_version": config_version,
        "chunk_signature_length": int(length),
        "similarity_threshold": float(threshold),
        "candidate_window": int(window),
        "validation_mode": mode,
        "sampling_seed": int(seed),
        "allowed_langs": tuple(allowed_langs),
        "bucket_strategy": bucket_strategy,
        "bucket_anchor_count": bucket_anchor_count,
        "bucket_prefix_chars": bucket_prefix_chars,
        "limits": limits,
        "latency_model": latency_model,
        "stage_versions": dict(stage_versions),
        "extras": extras,
    }


def load_config(path: str | Path) -> dict[str, Any]:
    return parse_config(Path(path))


SIGNATURE_FIELDS = ("chunk_signature_length", "normalization_version", "signature_algorithm")
CANDIDATE_FIELDS = ("candidate_window", "bucket_strategy", "bucket_anchor_count", "bucket_prefix_chars")
COMPARISON_FIELDS = ("validation_mode", "similarity_formula_version")
DECISION_FIELDS = ("similarity_threshold", "decision_policy_version")


def stage_parameters(config: dict[str, Any], stage: str) -> dict[str, Any]:
    versions = {
        "normalization_version": 1,
        "signature_algorithm": "sha256-nonoverlap-v1",
        "similarity_formula_version": "multiset-overlap-v1",
        "decision_policy_version": "dsu-quality-representative-v1",
    }
    fields = {
        "signature": SIGNATURE_FIELDS,
        "candidate": CANDIDATE_FIELDS,
        "comparison": COMPARISON_FIELDS,
        "decision": DECISION_FIELDS,
    }[stage]
    result = {field: config.get(field, versions.get(field)) for field in fields}
    result.update({key: versions[key] for key in fields if key in versions})
    result["config_version"] = int(config.get("stage_versions", {}).get(stage, 1))
    return result
