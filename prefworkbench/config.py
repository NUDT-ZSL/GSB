"""Configuration defaults and structured validation."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .canonical import atomic_write_text, canonical_json, read_json
from .errors import (
    BusinessError,
    E_CONFIG_FIXED_SCALE,
    E_CONFIG_INVALID,
    E_CONFIG_PRECISION,
    E_CONFIG_ROUNDING,
    E_CONFIG_SMOOTHING,
    E_CONFIG_THRESHOLD,
    E_CONFIG_WEIGHT,
)


PRECISIONS = ("float64", "float32", "fixed")
ROUNDING_MODES = ("half_even", "half_up", "toward_zero", "floor", "ceil")
MAX_FIXED_SCALE = 10**12


def default_config(seed: int = 20261122) -> dict[str, Any]:
    return {
        "seed": seed,
        "quality": {
            "weights": {"length": 0.40, "repetition": 0.35, "noise": 0.25},
            "length_target": 650,
            "shingle_size": 3,
            "quality_threshold": 0.55,
        },
        "near_duplicate": {
            "signature_seed": 9017,
            "signature_size": 32,
            "shingle_size": 3,
            "similarity_threshold": 0.72,
            "max_candidates": 40000,
        },
        "consistency": {
            "smoothing_alpha": 1.0,
            "prior": 0.72,
            "min_denominator": 0.25,
            "epsilon": 1e-12,
            "mad_scale": 4.0,
        },
        "sampling": {
            "seed": 424242,
            "total_target": 24,
            "group_quotas": {"g-alpha": 8, "g-beta": 8, "g-gamma": 8},
            "quality_quotas": {"low": 5, "medium": 10, "high": 9},
            "quality_bands": {"low": 0.0, "medium": 0.72, "high": 0.86},
        },
        "precision": {
            "active": "float64",
            "fixed_scale": 1000000,
            "rounding": "half_even",
            "subnormal_floor": 1e-30,
            "subnormal_protection": True,
            "compare_strategy": "tolerance",
            "quality_tolerance": 1e-9,
            "similarity_tolerance": 1e-9,
        },
        "limits": {
            "max_samples": 500,
            "max_signature_candidates": 40000,
            "max_manifest_bytes": 4000000,
        },
    }


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    """Fill omitted optional sections without changing explicitly supplied values."""

    base = default_config()
    result = copy.deepcopy(base)

    def merge(target: dict[str, Any], source: dict[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    merge(result, config)
    return result


def load_config_file(path: str | Path) -> dict[str, Any]:
    try:
        raw = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(
            E_CONFIG_INVALID,
            f"cannot read configuration: {exc}",
            field="config",
            stage="config_validation",
        ) from exc
    if not isinstance(raw, dict):
        raise BusinessError(
            E_CONFIG_INVALID,
            "configuration root must be an object",
            field="config",
            stage="config_validation",
        )
    return raw


def config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


def write_config(path: str | Path, config: dict[str, Any]) -> None:
    text = json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, text)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _error(code: str, field: str, message: str, details: dict[str, Any] | None = None) -> BusinessError:
    return BusinessError(
        code,
        message,
        field=field,
        stage="config_validation",
        details=details or {},
    )


def validate_config(config: dict[str, Any]) -> list[BusinessError]:
    """Return all configuration errors instead of stopping at the first one."""

    errors: list[BusinessError] = []
    if not isinstance(config, dict):
        return [_error(E_CONFIG_INVALID, "config", "configuration root must be an object")]

    seed = config.get("seed")
    if not _is_int(seed):
        errors.append(_error(E_CONFIG_INVALID, "seed", "seed must be an integer"))

    quality = config.get("quality")
    if not isinstance(quality, dict):
        errors.append(_error(E_CONFIG_INVALID, "quality", "quality section is required"))
        quality = {}
    weights = quality.get("weights")
    if not isinstance(weights, dict):
        errors.append(_error(E_CONFIG_WEIGHT, "quality.weights", "weights must be an object"))
    else:
        for name in ("length", "repetition", "noise"):
            value = weights.get(name)
            if not _is_number(value) or value <= 0:
                errors.append(
                    _error(
                        E_CONFIG_WEIGHT,
                        f"quality.weights.{name}",
                        "weight must be a finite positive number",
                        {"value": value},
                    )
                )
    for field in ("length_target", "shingle_size"):
        value = quality.get(field)
        if field == "shingle_size":
            if not _is_int(value) or value < 1:
                errors.append(_error(E_CONFIG_INVALID, f"quality.{field}", "must be an integer >= 1"))
        elif not _is_number(value) or value <= 0:
            errors.append(_error(E_CONFIG_INVALID, f"quality.{field}", "must be a finite positive number"))
    quality_threshold = quality.get("quality_threshold")
    if not _is_number(quality_threshold) or not 0 <= quality_threshold <= 1:
        errors.append(
            _error(
                E_CONFIG_THRESHOLD,
                "quality.quality_threshold",
                "quality threshold must be in [0, 1]",
                {"value": quality_threshold},
            )
        )

    near = config.get("near_duplicate")
    if not isinstance(near, dict):
        errors.append(_error(E_CONFIG_INVALID, "near_duplicate", "near_duplicate section is required"))
        near = {}
    similarity_threshold = near.get("similarity_threshold")
    if not _is_number(similarity_threshold) or not 0 <= similarity_threshold <= 1:
        errors.append(
            _error(
                E_CONFIG_THRESHOLD,
                "near_duplicate.similarity_threshold",
                "similarity threshold must be in [0, 1]",
                {"value": similarity_threshold},
            )
        )
    for field in ("signature_size", "shingle_size", "max_candidates"):
        value = near.get(field)
        if not _is_int(value) or value < 1:
            errors.append(_error(E_CONFIG_INVALID, f"near_duplicate.{field}", "must be an integer >= 1"))
    if not _is_int(near.get("signature_seed")):
        errors.append(_error(E_CONFIG_INVALID, "near_duplicate.signature_seed", "must be an integer"))

    consistency = config.get("consistency")
    if not isinstance(consistency, dict):
        errors.append(_error(E_CONFIG_INVALID, "consistency", "consistency section is required"))
        consistency = {}
    alpha = consistency.get("smoothing_alpha")
    if not _is_number(alpha) or alpha <= 0:
        errors.append(
            _error(
                E_CONFIG_SMOOTHING,
                "consistency.smoothing_alpha",
                "smoothing alpha must be a finite positive number",
                {"value": alpha},
            )
        )
    prior = consistency.get("prior")
    if not _is_number(prior) or not 0 <= prior <= 1:
        errors.append(
            _error(
                E_CONFIG_SMOOTHING,
                "consistency.prior",
                "consistency prior must be in [0, 1]",
                {"value": prior},
            )
        )
    denominator_floor = consistency.get("min_denominator")
    if not _is_number(denominator_floor) or denominator_floor <= 0:
        errors.append(
            _error(
                E_CONFIG_SMOOTHING,
                "consistency.min_denominator",
                "minimum smoothing denominator must be a finite positive number",
                {"value": denominator_floor},
            )
        )
    epsilon = consistency.get("epsilon")
    if not _is_number(epsilon) or epsilon < 0:
        errors.append(
            _error(
                E_CONFIG_SMOOTHING,
                "consistency.epsilon",
                "smoothing epsilon must be a finite non-negative number",
                {"value": epsilon},
            )
        )
    if not _is_number(consistency.get("mad_scale")) or consistency.get("mad_scale") <= 0:
        errors.append(
            _error(
                E_CONFIG_SMOOTHING,
                "consistency.mad_scale",
                "MAD scale must be a finite positive number",
                {"value": consistency.get("mad_scale")},
            )
        )

    sampling = config.get("sampling")
    if not isinstance(sampling, dict):
        errors.append(_error(E_CONFIG_INVALID, "sampling", "sampling section is required"))
        sampling = {}
    if not _is_int(sampling.get("seed")):
        errors.append(_error(E_CONFIG_INVALID, "sampling.seed", "sampling seed must be an integer"))
    if not _is_int(sampling.get("total_target")) or sampling.get("total_target") < 0:
        errors.append(_error(E_CONFIG_INVALID, "sampling.total_target", "must be an integer >= 0"))
    group_quotas = sampling.get("group_quotas")
    if not isinstance(group_quotas, dict) or not group_quotas:
        errors.append(_error(E_CONFIG_INVALID, "sampling.group_quotas", "must be a non-empty object"))
    else:
        for name, value in group_quotas.items():
            if not isinstance(name, str) or not name:
                errors.append(_error(E_CONFIG_INVALID, "sampling.group_quotas", "group names must be non-empty strings"))
            if not _is_int(value) or value < 0:
                errors.append(
                    _error(
                        E_CONFIG_INVALID,
                        f"sampling.group_quotas.{name}",
                        "quota must be an integer >= 0",
                        {"value": value},
                    )
                )
    quality_quotas = sampling.get("quality_quotas")
    if not isinstance(quality_quotas, dict) or not quality_quotas:
        errors.append(_error(E_CONFIG_INVALID, "sampling.quality_quotas", "must be a non-empty object"))
    else:
        for name, value in quality_quotas.items():
            if not isinstance(name, str) or not name:
                errors.append(_error(E_CONFIG_INVALID, "sampling.quality_quotas", "quality names must be non-empty strings"))
            if not _is_int(value) or value < 0:
                errors.append(
                    _error(
                        E_CONFIG_INVALID,
                        f"sampling.quality_quotas.{name}",
                        "quota must be an integer >= 0",
                        {"value": value},
                    )
                )
    bands = sampling.get("quality_bands")
    if not isinstance(bands, dict):
        errors.append(_error(E_CONFIG_INVALID, "sampling.quality_bands", "must be an object"))
    else:
        for name in ("low", "medium", "high"):
            value = bands.get(name)
            if not _is_number(value) or not 0 <= value <= 1:
                errors.append(_error(E_CONFIG_INVALID, f"sampling.quality_bands.{name}", "must be in [0, 1]"))
        if all(_is_number(bands.get(k)) for k in ("low", "medium", "high")):
            if not bands["low"] <= bands["medium"] <= bands["high"]:
                errors.append(
                    _error(
                        E_CONFIG_INVALID,
                        "sampling.quality_bands",
                        "band thresholds must satisfy low <= medium <= high",
                    )
                )

    precision = config.get("precision")
    if not isinstance(precision, dict):
        errors.append(_error(E_CONFIG_PRECISION, "precision", "precision section is required"))
        precision = {}
    if precision.get("active") not in PRECISIONS:
        errors.append(
            _error(
                E_CONFIG_PRECISION,
                "precision.active",
                "unknown precision profile",
                {"value": precision.get("active"), "allowed": list(PRECISIONS)},
            )
        )
    scale = precision.get("fixed_scale")
    if not _is_int(scale) or scale <= 0 or scale > MAX_FIXED_SCALE:
        errors.append(
            _error(
                E_CONFIG_FIXED_SCALE,
                "precision.fixed_scale",
                f"fixed scale must be an integer in [1, {MAX_FIXED_SCALE}]",
                {"value": scale},
            )
        )
    if precision.get("rounding") not in ROUNDING_MODES:
        errors.append(
            _error(
                E_CONFIG_ROUNDING,
                "precision.rounding",
                "unknown fixed-point rounding mode",
                {"value": precision.get("rounding"), "allowed": list(ROUNDING_MODES)},
            )
        )
    subnormal_floor = precision.get("subnormal_floor")
    if not _is_number(subnormal_floor) or subnormal_floor < 0:
        errors.append(
            _error(E_CONFIG_INVALID, "precision.subnormal_floor", "must be a finite non-negative number")
        )
    if not isinstance(precision.get("subnormal_protection"), bool):
        errors.append(
            _error(E_CONFIG_INVALID, "precision.subnormal_protection", "must be boolean")
        )
    if precision.get("compare_strategy") not in ("strict", "tolerance"):
        errors.append(
            _error(
                E_CONFIG_INVALID,
                "precision.compare_strategy",
                "must be 'strict' or 'tolerance'",
            )
        )
    for field in ("quality_tolerance", "similarity_tolerance"):
        value = precision.get(field)
        if not _is_number(value) or value < 0:
            errors.append(_error(E_CONFIG_THRESHOLD, f"precision.{field}", "must be finite and >= 0"))

    limits = config.get("limits")
    if not isinstance(limits, dict):
        errors.append(_error(E_CONFIG_INVALID, "limits", "limits section is required"))
        limits = {}
    for field in ("max_samples", "max_signature_candidates", "max_manifest_bytes"):
        value = limits.get(field)
        if not _is_int(value) or value < 1:
            errors.append(_error(E_CONFIG_INVALID, f"limits.{field}", "must be an integer >= 1"))

    return errors


def require_valid_config(config: dict[str, Any]) -> None:
    errors = validate_config(config)
    if errors:
        first = errors[0]
        raise BusinessError(
            first.code,
            first.message,
            field=first.field,
            stage=first.stage,
            details={"errors": [error.to_dict() for error in errors]},
        )

