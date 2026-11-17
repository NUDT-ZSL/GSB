"""Configuration parsing and validation."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .canonical import read_json, sha256_json

WEIGHT_KEYS = ("distribution_l1", "js_divergence", "novelty")

class ConfigError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError("E_CONFIG_INVALID", message)


def _require_string(value: Any, name: str) -> str:
    _require(isinstance(value, str) and bool(value.strip()), f"{name} must be a non-empty string")
    return value


def validate_weights(weights: Any) -> dict[str, float]:
    _require(isinstance(weights, dict), "weights must be an object")
    keys = set(weights)
    expected = set(WEIGHT_KEYS)
    _require(keys == expected, f"weights keys must be exactly {sorted(expected)}")
    normalized: dict[str, float] = {}
    for key in WEIGHT_KEYS:
        value = weights[key]
        _require(
            isinstance(value, (int, float)) and not isinstance(value, bool),
            f"weight {key} must be numeric",
        )
        numeric = float(value)
        _require(math.isfinite(numeric), f"weight {key} must be finite")
        _require(numeric >= 0.0, f"weight {key} must be non-negative")
        normalized[key] = numeric
    _require(
        abs(sum(normalized.values()) - 1.0) <= 1e-9,
        "weights must sum to 1.0",
    )
    return normalized


def vocabulary_terms(vocabulary: Any) -> list[str]:
    _require(isinstance(vocabulary, list), "vocabulary must be a list")
    terms: list[str] = []
    for index, item in enumerate(vocabulary):
        if isinstance(item, str):
            term = item
        elif isinstance(item, dict) and isinstance(item.get("term"), str):
            term = item["term"]
        else:
            raise ConfigError(
                "E_CONFIG_INVALID",
                f"vocabulary[{index}] must be a string or an object with a string term",
            )
        term = term.strip()
        _require(bool(term), f"vocabulary[{index}] term must be non-empty")
        terms.append(term)
    _require(len(terms) == len(set(terms)), "vocabulary terms must be unique")
    return sorted(terms)


def load_config(path: Path) -> dict[str, Any]:
    try:
        raw = read_json(path)
    except (OSError, ValueError) as exc:
        raise ConfigError("E_CONFIG_INVALID", f"cannot read config: {exc}") from exc
    _require(isinstance(raw, dict), "config root must be an object")

    categories_raw = raw.get("categories")
    _require(isinstance(categories_raw, list) and bool(categories_raw), "categories must be a non-empty list")
    categories: list[str] = []
    for index, category in enumerate(categories_raw):
        category = _require_string(category, f"categories[{index}]")
        categories.append(category)
    _require(len(categories) == len(set(categories)), "categories must be unique")
    categories = sorted(categories)

    fold_raw = raw.get("fold_rules")
    _require(isinstance(fold_raw, dict), "fold_rules must be an object")
    fold_rules = {
        "version": _require_string(fold_raw.get("version"), "fold_rules.version"),
        "lowercase": bool(fold_raw.get("lowercase", True)),
        "nfkc": bool(fold_raw.get("nfkc", True)),
        "strip_punctuation": bool(fold_raw.get("strip_punctuation", True)),
        "collapse_whitespace": bool(fold_raw.get("collapse_whitespace", True)),
    }

    window_size = raw.get("window_size")
    _require(isinstance(window_size, int) and not isinstance(window_size, bool) and window_size >= 1,
             "window_size must be an integer >= 1")
    precision = raw.get("precision")
    _require(isinstance(precision, int) and not isinstance(precision, bool) and 0 <= precision <= 12,
             "precision must be an integer in [0, 12]")
    concurrency = raw.get("concurrency")
    _require(isinstance(concurrency, int) and not isinstance(concurrency, bool) and concurrency >= 1,
             "concurrency must be an integer >= 1")
    snapshot_interval = raw.get("snapshot_interval")
    _require(isinstance(snapshot_interval, int) and not isinstance(snapshot_interval, bool)
             and snapshot_interval >= 1, "snapshot_interval must be an integer >= 1")
    threshold = raw.get("drift_threshold")
    _require(isinstance(threshold, (int, float)) and not isinstance(threshold, bool),
             "drift_threshold must be numeric")
    threshold = float(threshold)
    _require(math.isfinite(threshold) and 0.0 <= threshold <= 1.0,
             "drift_threshold must be finite and in [0, 1]")

    config = {
        "schema_version": int(raw.get("schema_version", 1)),
        "intent_vocab_version": _require_string(
            raw.get("intent_vocab_version"), "intent_vocab_version"
        ),
        "categories": categories,
        "vocabulary": vocabulary_terms(raw.get("vocabulary")),
        "fold_rules": fold_rules,
        "window_size": window_size,
        "weights": validate_weights(raw.get("weights")),
        "drift_threshold": threshold,
        "precision": precision,
        "concurrency": concurrency,
        "snapshot_interval": snapshot_interval,
    }
    return config


def static_config(config: dict[str, Any]) -> dict[str, Any]:
    # The on-disk config is immutable during a run. Runtime vocabulary and
    # weight changes are tracked separately in the snapshot manifest.
    return dict(config)


def config_hash(config: dict[str, Any]) -> str:
    return sha256_json(static_config(config))


def base_vocab_payload(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "intent_vocab_version": config["intent_vocab_version"],
        "categories": config["categories"],
        "vocabulary": sorted(config["vocabulary"]),
    }


def base_vocab_hash(config: dict[str, Any]) -> str:
    return sha256_json(base_vocab_payload(config))


def weights_hash(weights: dict[str, float]) -> str:
    return sha256_json({key: weights[key] for key in WEIGHT_KEYS})
