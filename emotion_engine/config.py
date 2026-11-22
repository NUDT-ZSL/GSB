"""Configuration loading and strict validation."""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import E_CONFIG_INVALID, EngineError
from .util import canonical_json, sha256_json


def _config_error(field: str, message: str) -> EngineError:
    return EngineError(
        E_CONFIG_INVALID,
        f"invalid configuration field {field}: {message}",
        field=field,
    )


def _require_number(mapping: dict[str, Any], field: str, path: str) -> float:
    value = mapping.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _config_error(path, "must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise _config_error(path, "must be finite")
    return value


def _require_int(mapping: dict[str, Any], field: str, path: str, minimum: int = 1) -> int:
    value = mapping.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _config_error(path, "must be an integer")
    if value < minimum:
        raise _config_error(path, f"must be >= {minimum}")
    return value


@dataclass(frozen=True)
class EngineConfig:
    raw: dict[str, Any]

    @property
    def schema_version(self) -> str:
        return str(self.raw["schema_version"])

    @property
    def feature_version(self) -> str:
        return str(self.raw["feature_version"])

    @property
    def tier_algorithm_version(self) -> str:
        return str(self.raw["tier_algorithm_version"])

    @property
    def frame_ms(self) -> int:
        return int(self.raw["frame_ms"])

    @property
    def hop_ms(self) -> int:
        return int(self.raw["hop_ms"])

    @property
    def window(self) -> dict[str, Any]:
        return self.raw["window"]

    @property
    def threshold(self) -> dict[str, Any]:
        return self.raw["threshold"]

    @property
    def feature(self) -> dict[str, Any]:
        return self.raw["feature"]

    @property
    def tier_schema(self) -> dict[str, Any]:
        return self.raw["tier_schema"]

    @property
    def comparison_abs_tol(self) -> float:
        return float(self.raw["comparison"]["abs_tol"])

    @property
    def declared_precision(self) -> str:
        return str(self.raw["comparison"]["declared_precision"])

    @property
    def subnormal_guard(self) -> float:
        return float(self.threshold["subnormal_guard"])

    @property
    def score_decimals(self) -> int:
        return int(self.feature["score_decimals"])

    @property
    def window_length(self) -> int:
        return int(self.window["length_frames"])

    @property
    def tie_rule(self) -> str:
        return str(self.window["tie_rule"])

    @property
    def strategy(self) -> str:
        return str(self.threshold["strategy"])

    @property
    def tolerance(self) -> float:
        return float(self.threshold["tolerance"])

    def frame_samples(self, sample_rate: int) -> int:
        numerator = sample_rate * self.frame_ms
        if numerator % 1000:
            raise _config_error("frame_ms", "does not map to an integer sample count")
        return numerator // 1000

    def hop_samples(self, sample_rate: int) -> int:
        numerator = sample_rate * self.hop_ms
        if numerator % 1000:
            raise _config_error("hop_ms", "does not map to an integer sample count")
        return numerator // 1000

    def tier_schema_hash(self) -> str:
        return sha256_json(self.tier_schema)

    def threshold_policy_signature(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "tolerance": self.tolerance,
            "subnormal_guard": self.subnormal_guard,
            "tier_algorithm_version": self.tier_algorithm_version,
            "tier_schema_hash": self.tier_schema_hash(),
        }

    def window_signature(self) -> dict[str, Any]:
        return {
            "version": str(self.window["version"]),
            "length_frames": self.window_length,
            "boundary_policy": str(self.window["boundary_policy"]),
            "insufficient_policy": str(self.window["insufficient_policy"]),
            "tie_rule": self.tie_rule,
        }

    def feature_signature(self) -> dict[str, Any]:
        return {
            "version": self.feature_version,
            "frame_ms": self.frame_ms,
            "hop_ms": self.hop_ms,
            "subnormal_guard": self.subnormal_guard,
            "parameters": copy.deepcopy(self.feature),
        }

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)

    def canonical(self) -> str:
        return canonical_json(self.raw)

    def content_hash(self) -> str:
        return sha256_json(self.raw)


def _validate_tier_schema(schema: dict[str, Any]) -> None:
    if not isinstance(schema, dict):
        raise _config_error("tier_schema", "must be an object")
    if not isinstance(schema.get("version"), str) or not schema["version"]:
        raise _config_error("tier_schema.version", "must be a non-empty string")
    valence_thresholds = schema.get("valence_thresholds")
    if (
        not isinstance(valence_thresholds, list)
        or len(valence_thresholds) != 2
        or not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in valence_thresholds)
    ):
        raise _config_error("tier_schema.valence_thresholds", "must contain two numbers")
    low, high = float(valence_thresholds[0]), float(valence_thresholds[1])
    if not (math.isfinite(low) and math.isfinite(high) and low < high):
        raise _config_error("tier_schema.valence_thresholds", "must be finite and ascending")
    arousal = schema.get("arousal_threshold")
    if isinstance(arousal, bool) or not isinstance(arousal, (int, float)) or not math.isfinite(float(arousal)):
        raise _config_error("tier_schema.arousal_threshold", "must be finite")
    valence_bands = schema.get("valence_bands")
    arousal_bands = schema.get("arousal_bands")
    if valence_bands != ["negative", "neutral", "positive"]:
        raise _config_error("tier_schema.valence_bands", "must be negative/neutral/positive")
    if arousal_bands != ["low", "high"]:
        raise _config_error("tier_schema.arousal_bands", "must be low/high")
    tier_labels = schema.get("tier_labels")
    if not isinstance(tier_labels, list) or not tier_labels or len(set(tier_labels)) != len(tier_labels):
        raise _config_error("tier_schema.tier_labels", "must be a non-empty unique list")
    if not all(isinstance(item, str) and item for item in tier_labels):
        raise _config_error("tier_schema.tier_labels", "items must be non-empty strings")
    tier_map = schema.get("tier_map")
    if not isinstance(tier_map, list):
        raise _config_error("tier_schema.tier_map", "must be a list")
    expected = {
        (valence, arousal)
        for valence in valence_bands
        for arousal in arousal_bands
    }
    seen: set[tuple[str, str]] = set()
    mapped_tiers: set[str] = set()
    for index, item in enumerate(tier_map):
        if not isinstance(item, dict):
            raise _config_error(f"tier_schema.tier_map[{index}]", "must be an object")
        key = (item.get("valence_band"), item.get("arousal_band"))
        tier = item.get("tier")
        if key not in expected:
            raise _config_error(f"tier_schema.tier_map[{index}]", "has an unknown band pair")
        if key in seen:
            raise _config_error(f"tier_schema.tier_map[{index}]", "duplicates a band pair")
        if tier not in tier_labels:
            raise _config_error(f"tier_schema.tier_map[{index}].tier", "must be present in tier_labels")
        seen.add(key)
        mapped_tiers.add(str(tier))
    if seen != expected:
        raise _config_error("tier_schema.tier_map", "must map all six band pairs")
    if mapped_tiers != set(tier_labels):
        raise _config_error("tier_schema.tier_labels", "must equal the set of mapped tiers")


def config_from_dict(raw: dict[str, Any]) -> EngineConfig:
    if not isinstance(raw, dict):
        raise _config_error("<root>", "must be an object")
    raw = copy.deepcopy(raw)
    for field in ("schema_version", "feature_version", "tier_algorithm_version"):
        if not isinstance(raw.get(field), str) or not raw[field]:
            raise _config_error(field, "must be a non-empty string")
    raw["frame_ms"] = _require_int(raw, "frame_ms", "frame_ms")
    raw["hop_ms"] = _require_int(raw, "hop_ms", "hop_ms")

    window = raw.get("window")
    if not isinstance(window, dict):
        raise _config_error("window", "must be an object")
    _require_int(window, "length_frames", "window.length_frames")
    for field in ("version", "boundary_policy", "insufficient_policy", "tie_rule"):
        if not isinstance(window.get(field), str) or not window[field]:
            raise _config_error(f"window.{field}", "must be a non-empty string")
    if window["boundary_policy"] != "truncate_left_no_pad":
        raise _config_error("window.boundary_policy", "only truncate_left_no_pad is supported")
    if window["insufficient_policy"] != "use_available_frames_no_fill":
        raise _config_error("window.insufficient_policy", "only use_available_frames_no_fill is supported")
    if window["tie_rule"] != "tier_schema_order":
        raise _config_error("window.tie_rule", "only tier_schema_order is supported")

    threshold = raw.get("threshold")
    if not isinstance(threshold, dict):
        raise _config_error("threshold", "must be an object")
    if threshold.get("strategy") not in {"strict", "tolerance"}:
        raise _config_error("threshold.strategy", "must be 'strict' or 'tolerance'")
    threshold["tolerance"] = _require_number(threshold, "tolerance", "threshold.tolerance")
    if threshold["tolerance"] < 0:
        raise _config_error("threshold.tolerance", "must be >= 0")
    threshold["subnormal_guard"] = _require_number(threshold, "subnormal_guard", "threshold.subnormal_guard")
    if threshold["subnormal_guard"] < 0:
        raise _config_error("threshold.subnormal_guard", "must be >= 0")

    feature = raw.get("feature")
    if not isinstance(feature, dict):
        raise _config_error("feature", "must be an object")
    for field in (
        "min_f0_hz",
        "max_f0_hz",
        "speech_energy_db",
        "energy_floor_db",
        "energy_ceiling_db",
        "zcr_reference",
        "autocorrelation_floor",
    ):
        feature[field] = _require_number(feature, field, f"feature.{field}")
    if feature["min_f0_hz"] <= 0 or feature["max_f0_hz"] <= feature["min_f0_hz"]:
        raise _config_error("feature.max_f0_hz", "must be greater than feature.min_f0_hz")
    if feature["zcr_reference"] <= 0:
        raise _config_error("feature.zcr_reference", "must be > 0")
    if not 0 <= feature["autocorrelation_floor"] <= 1:
        raise _config_error("feature.autocorrelation_floor", "must be in [0, 1]")
    if feature["energy_floor_db"] >= feature["energy_ceiling_db"]:
        raise _config_error("feature.energy_ceiling_db", "must be greater than feature.energy_floor_db")
    feature["score_decimals"] = _require_int(feature, "score_decimals", "feature.score_decimals", 0)

    comparison = raw.get("comparison")
    if not isinstance(comparison, dict):
        raise _config_error("comparison", "must be an object")
    comparison["abs_tol"] = _require_number(comparison, "abs_tol", "comparison.abs_tol")
    if comparison["abs_tol"] < 0:
        raise _config_error("comparison.abs_tol", "must be >= 0")
    if not isinstance(comparison.get("declared_precision"), str) or not comparison["declared_precision"]:
        raise _config_error("comparison.declared_precision", "must be a non-empty string")

    estimate = raw.get("latency_estimate")
    if not isinstance(estimate, dict):
        raise _config_error("latency_estimate", "must be an object")
    for field in ("frame_factor_ms", "window_factor_ms", "speaker_factor_ms"):
        estimate[field] = _require_number(estimate, field, f"latency_estimate.{field}")
        if estimate[field] < 0:
            raise _config_error(f"latency_estimate.{field}", "must be >= 0")

    cache = raw.get("cache")
    if not isinstance(cache, dict) or cache.get("mode") != "process_memory":
        raise _config_error("cache.mode", "only process_memory is supported")
    if cache.get("content_addressing") != "sha256_canonical_json":
        raise _config_error(
            "cache.content_addressing",
            "only sha256_canonical_json is supported",
        )

    _validate_tier_schema(raw.get("tier_schema"))
    return EngineConfig(raw=raw)


def load_config(path: str | Path) -> EngineConfig:
    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise _config_error("path", f"configuration file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise _config_error("json", str(exc)) from exc
    return config_from_dict(raw)
