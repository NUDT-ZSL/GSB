from __future__ import annotations

import copy
import math
import re
from typing import Any

from .io_utils import read_json, sha256_obj


class ConfigError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


DEFAULT_CONFIG = {
    "subword_rule": "word_v1",
    "subword_pattern": r"[a-z0-9]+",
    "normalize_text": True,
    "normalization_version": "nfkc_casefold_whitespace_v1",
    "feature_dimensions": 24,
    "hash_buckets": 24,
    "hash_function": "blake2b_64_v1",
    "projection": "bucket_mod_dimensions_v1",
    "weights": [0.1] * 24,
    "score_precision": 6,
    "reward_delta_threshold": 0.15,
    "concurrency": 4,
    "snapshot_commit_interval": 1,
}


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("E_CONFIG_INVALID", f"{name} must be an object")
    return value


def _required_int(mapping: dict[str, Any], name: str, minimum: int, code: str) -> int:
    value = mapping.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(code, f"{name} must be an integer >= {minimum}")
    return value


def _validate_weights(value: Any, dimensions: int) -> list[float]:
    if not isinstance(value, list):
        raise ConfigError("E_WEIGHT_INVALID", "weights must be a JSON array")
    if len(value) != dimensions:
        raise ConfigError(
            "E_WEIGHT_INVALID",
            f"weights length {len(value)} does not match feature_dimensions {dimensions}",
        )
    weights: list[float] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ConfigError("E_WEIGHT_INVALID", f"weights[{index}] is not numeric")
        number = float(item)
        if not math.isfinite(number):
            raise ConfigError("E_WEIGHT_INVALID", f"weights[{index}] is not finite")
        weights.append(number)
    return weights


def validate_config(raw: dict[str, Any]) -> dict[str, Any]:
    mapping = _require_mapping(raw, "feature config")
    config = dict(DEFAULT_CONFIG)
    for key in DEFAULT_CONFIG:
        if key in mapping:
            config[key] = mapping[key]

    if not isinstance(config["subword_rule"], str) or not config["subword_rule"].strip():
        raise ConfigError("E_CONFIG_INVALID", "subword_rule must be a non-empty string")
    if not isinstance(config["subword_pattern"], str) or not config["subword_pattern"]:
        raise ConfigError("E_CONFIG_INVALID", "subword_pattern must be a non-empty string")
    try:
        re.compile(config["subword_pattern"])
    except re.error as exc:
        raise ConfigError("E_CONFIG_INVALID", f"subword_pattern is invalid: {exc}") from exc
    if not isinstance(config["normalize_text"], bool):
        raise ConfigError("E_CONFIG_INVALID", "normalize_text must be boolean")
    if (
        not isinstance(config["normalization_version"], str)
        or not config["normalization_version"].strip()
    ):
        raise ConfigError("E_CONFIG_INVALID", "normalization_version must be a non-empty string")

    config["feature_dimensions"] = _required_int(
        config, "feature_dimensions", 1, "E_CONFIG_INVALID"
    )
    config["hash_buckets"] = _required_int(config, "hash_buckets", 1, "E_CONFIG_INVALID")
    config["score_precision"] = _required_int(
        config, "score_precision", 0, "E_CONFIG_INVALID"
    )
    if config["score_precision"] > 12:
        raise ConfigError("E_CONFIG_INVALID", "score_precision must be <= 12")
    config["concurrency"] = _required_int(config, "concurrency", 1, "E_CONFIG_INVALID")
    config["snapshot_commit_interval"] = _required_int(
        config, "snapshot_commit_interval", 1, "E_CONFIG_INVALID"
    )

    threshold = config["reward_delta_threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ConfigError("E_CONFIG_INVALID", "reward_delta_threshold must be numeric")
    threshold = float(threshold)
    if not math.isfinite(threshold) or threshold < 0:
        raise ConfigError(
            "E_CONFIG_INVALID", "reward_delta_threshold must be finite and >= 0"
        )
    config["reward_delta_threshold"] = threshold

    config["hash_function"] = str(config["hash_function"])
    config["projection"] = str(config["projection"])
    if config["hash_function"] != "blake2b_64_v1":
        raise ConfigError("E_CONFIG_INVALID", "only blake2b_64_v1 is supported")
    if config["projection"] != "bucket_mod_dimensions_v1":
        raise ConfigError("E_CONFIG_INVALID", "only bucket_mod_dimensions_v1 is supported")

    config["weights"] = _validate_weights(
        config["weights"], config["feature_dimensions"]
    )
    return config


def load_config(path: str) -> dict[str, Any]:
    return validate_config(read_json(path))


def feature_projection(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "subword_rule": config["subword_rule"],
        "subword_pattern": config["subword_pattern"],
        "normalize_text": config["normalize_text"],
        "normalization_version": config["normalization_version"],
        "feature_dimensions": config["feature_dimensions"],
        "hash_buckets": config["hash_buckets"],
        "hash_function": config["hash_function"],
        "projection": config["projection"],
    }


def scoring_projection(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "weights": list(config["weights"]),
        "score_precision": config["score_precision"],
        "reward_delta_threshold": config["reward_delta_threshold"],
    }


def feature_config_hash(config: dict[str, Any]) -> str:
    return sha256_obj(feature_projection(config))


def config_hash(config: dict[str, Any]) -> str:
    normalized = copy.deepcopy(config)
    return sha256_obj(normalized)


def semantic_config_hash(config: dict[str, Any]) -> str:
    semantic = copy.deepcopy(config)
    semantic.pop("concurrency", None)
    semantic.pop("snapshot_commit_interval", None)
    return sha256_obj(semantic)


def apply_config_updates(config: dict[str, Any], batch: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(config)
    if "weights" in batch:
        updated["weights"] = batch["weights"]
    if "weight_updates" in batch:
        updates = batch["weight_updates"]
        if not isinstance(updates, dict):
            raise ConfigError("E_WEIGHT_INVALID", "weight_updates must be an object")
        weights = list(updated.get("weights", []))
        for key, value in updates.items():
            try:
                index = int(key)
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    "E_WEIGHT_INVALID", f"weight_updates key {key!r} is not an index"
                ) from exc
            if index < 0 or index >= len(weights):
                raise ConfigError(
                    "E_WEIGHT_INVALID", f"weight_updates index {index} is out of range"
                )
            weights[index] = value
        updated["weights"] = weights
    if "reward_delta_threshold" in batch:
        updated["reward_delta_threshold"] = batch["reward_delta_threshold"]
    if "score_precision" in batch:
        updated["score_precision"] = batch["score_precision"]
    if "concurrency" in batch:
        updated["concurrency"] = batch["concurrency"]
    if "snapshot_commit_interval" in batch:
        updated["snapshot_commit_interval"] = batch["snapshot_commit_interval"]

    immutable = (
        "subword_rule",
        "subword_pattern",
        "normalize_text",
        "normalization_version",
        "feature_dimensions",
        "hash_buckets",
        "hash_function",
        "projection",
    )
    for field in immutable:
        if field in batch and batch[field] != config.get(field):
            raise ConfigError(
                "E_PLAN_INVALID",
                f"batch cannot change extraction field {field}; rebuild the base config",
            )
    return validate_config(updated)
