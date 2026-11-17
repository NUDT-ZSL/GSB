"""Configuration loading, normalization, and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .constants import (
    CLASSIFIERS,
    E_INVALID_GRANULARITY,
    FEATURE_NAMES,
)
from .errors import ConfigError
from .util import load_json, stable_hash


DEFAULTS: dict[str, Any] = {
    "schema_version": 1,
    "frame_length_ms": 25,
    "frame_shift_ms": 10,
    "acoustic_feature_set": list(FEATURE_NAMES),
    "emotion_granularity": "fine",
    "classifier": "auto",
    "boundary_margin": 0.03,
    "reference_precision": 0.05,
    "fusion": {
        "window": 5,
        "window_mode": "adaptive",
        "decay": 0.65,
        "same_speaker_weight": 1.0,
        "cross_speaker_weight": 0.55,
        "missing_predecessor_rule": "nearest_available",
    },
    "feature_dimension_limit": 12,
    "scale_tier": "auto",
    "scale_tiers": {
        "small_max_units": 90,
        "medium_max_units": 1600,
        "duration_weight": 10,
    },
    "concurrency": 4,
    "snapshot_interval": 8,
}


def _merge_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    merged = {key: value for key, value in DEFAULTS.items() if not isinstance(value, dict)}
    merged["fusion"] = dict(DEFAULTS["fusion"])
    merged["scale_tiers"] = dict(DEFAULTS["scale_tiers"])
    for key, value in raw.items():
        if key in {"fusion", "scale_tiers"} and isinstance(value, dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def normalize_config(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a JSON object")
    config = _merge_defaults(raw)
    if config["schema_version"] != 1:
        raise ConfigError(f"unsupported config schema_version: {config['schema_version']}")
    for key in ("frame_length_ms", "frame_shift_ms"):
        value = config[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"{key} must be a positive integer")
    if config["frame_shift_ms"] > 500:
        raise ConfigError("frame_shift_ms is unreasonably large")
    features = config.get("acoustic_feature_set")
    if not isinstance(features, list) or not features:
        raise ConfigError("acoustic_feature_set must be a non-empty list")
    feature_order: list[str] = []
    for name in features:
        if name not in FEATURE_NAMES:
            raise ConfigError(f"unknown acoustic feature: {name}")
        if name not in feature_order:
            feature_order.append(name)
    config["acoustic_feature_set"] = feature_order
    granularity = config.get("emotion_granularity")
    if granularity not in {"coarse", "fine"}:
        raise ConfigError(
            f"emotion_granularity must be coarse or fine, got {granularity!r}",
            code=E_INVALID_GRANULARITY,
        )
    classifier = config.get("classifier")
    if classifier not in {"auto", *CLASSIFIERS}:
        raise ConfigError(f"classifier must be auto, {CLASSIFIERS[0]}, or {CLASSIFIERS[1]}")
    limit = config.get("feature_dimension_limit")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ConfigError("feature_dimension_limit must be a positive integer")
    for key in ("boundary_margin", "reference_precision"):
        value = config.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) < 0:
            raise ConfigError(f"{key} must be a non-negative number")
    fusion = config["fusion"]
    for key in ("window",):
        if not isinstance(fusion.get(key), int) or isinstance(fusion.get(key), bool) or fusion[key] < 0:
            raise ConfigError(f"fusion.{key} must be a non-negative integer")
    if fusion.get("window_mode") not in {"adaptive", "fixed"}:
        raise ConfigError("fusion.window_mode must be adaptive or fixed")
    decay = fusion.get("decay")
    if not isinstance(decay, (int, float)) or isinstance(decay, bool) or not 0 < float(decay) <= 1:
        raise ConfigError("fusion.decay must be in (0, 1]")
    for key in ("same_speaker_weight", "cross_speaker_weight"):
        weight = fusion.get(key)
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or float(weight) < 0:
            raise ConfigError(f"fusion.{key} must be non-negative")
    if fusion.get("missing_predecessor_rule") not in {"nearest_available", "zero_context"}:
        raise ConfigError("fusion.missing_predecessor_rule must be nearest_available or zero_context")
    if config.get("scale_tier") not in {"auto", "small", "medium", "large"}:
        raise ConfigError("scale_tier must be auto, small, medium, or large")
    tiers = config["scale_tiers"]
    for key in ("small_max_units", "medium_max_units", "duration_weight"):
        value = tiers.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) <= 0:
            raise ConfigError(f"scale_tiers.{key} must be positive")
    if float(tiers["small_max_units"]) >= float(tiers["medium_max_units"]):
        raise ConfigError("scale_tiers.small_max_units must be below medium_max_units")
    for key in ("concurrency", "snapshot_interval"):
        value = config.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"{key} must be a positive integer")
    if config["concurrency"] > 256:
        raise ConfigError("concurrency must not exceed 256")
    normalized = {
        "schema_version": 1,
        "frame_length_ms": int(config["frame_length_ms"]),
        "frame_shift_ms": int(config["frame_shift_ms"]),
        "acoustic_feature_set": feature_order,
        "emotion_granularity": granularity,
        "classifier": classifier,
        "boundary_margin": float(config["boundary_margin"]),
        "reference_precision": float(config["reference_precision"]),
        "fusion": {
            "window": int(fusion["window"]),
            "window_mode": fusion["window_mode"],
            "decay": float(decay),
            "same_speaker_weight": float(fusion["same_speaker_weight"]),
            "cross_speaker_weight": float(fusion["cross_speaker_weight"]),
            "missing_predecessor_rule": fusion["missing_predecessor_rule"],
        },
        "feature_dimension_limit": int(limit),
        "scale_tier": config["scale_tier"],
        "scale_tiers": {
            "small_max_units": float(tiers["small_max_units"]),
            "medium_max_units": float(tiers["medium_max_units"]),
            "duration_weight": float(tiers["duration_weight"]),
        },
        "concurrency": int(config["concurrency"]),
        "snapshot_interval": int(config["snapshot_interval"]),
    }
    normalized["config_fingerprint"] = stable_hash(normalized)
    return normalized


def load_config(path: Path) -> dict[str, Any]:
    try:
        raw = load_json(path)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read config JSON: {exc}") from exc
    return normalize_config(raw)


INJECTION_KINDS = {
    "nonfinite_sample",
    "zero_duration",
    "bad_audio_format",
    "missing_predecessor",
    "invalid_granularity",
}


def normalize_injections(raw: Any) -> dict[str, Any]:
    if raw is None:
        items: list[dict] = []
        schema_version = 1
    elif isinstance(raw, list):
        items = raw
        schema_version = 1
    elif isinstance(raw, dict):
        schema_version = raw.get("schema_version", 1)
        items = raw.get("injections", [])
    else:
        raise ConfigError("injection document must be an object or list")
    if schema_version != 1:
        raise ConfigError(f"unsupported injection schema_version: {schema_version}")
    if not isinstance(items, list):
        raise ConfigError("injections must be a list")
    normalized: list[dict] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ConfigError(f"injection at index {index} is not an object")
        injection_id = str(item.get("id", ""))
        kind = str(item.get("kind", ""))
        target = str(item.get("target", ""))
        if not injection_id or injection_id in seen_ids:
            raise ConfigError(f"injection id is missing or duplicated at index {index}")
        if kind not in INJECTION_KINDS:
            raise ConfigError(f"unsupported injection kind {kind!r} for {injection_id}")
        seen_ids.add(injection_id)
        normalized_item = {
            "id": injection_id,
            "kind": kind,
            "target": target,
            "phase": str(item.get("phase", "")),
        }
        if "value" in item:
            normalized_item["value"] = item["value"]
        normalized.append(normalized_item)
    normalized.sort(key=lambda item: item["id"])
    document = {"schema_version": 1, "injections": normalized}
    document["injection_fingerprint"] = stable_hash(document)
    return document


def load_injections(path: Path | None) -> dict[str, Any]:
    if path is None:
        return normalize_injections(None)
    try:
        return normalize_injections(load_json(path))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read injection JSON: {exc}") from exc
