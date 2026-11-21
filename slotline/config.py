from __future__ import annotations

import copy
from typing import Any

from .canonical import sha256_json
from .errors import BusinessError, E_INVALID_CONFIG


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "scoring_algorithm": "confidence_x_extractor_weight.v1",
    "score_decimals": 6,
    "extractor_versions": {
        "v1": 1.0,
        "v2": 1.05,
        "v3": 1.12,
    },
    "clarification_threshold": 0.65,
    "merge_policy": "source_turn_desc,score_desc,value_asc",
    "pending_policy": "strict_less_than_threshold",
    "simultaneous_order": ["retract", "append"],
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def normalize_config(override: dict[str, Any] | None = None) -> dict[str, Any]:
    config = _deep_merge(DEFAULT_CONFIG, override or {})
    _validate_config(config)
    return config


def _validate_config(config: dict[str, Any]) -> None:
    threshold = config.get("clarification_threshold")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise BusinessError(
            E_INVALID_CONFIG,
            "clarification_threshold must be a number",
            node="config",
        )
    if not 0 <= float(threshold) <= 1:
        raise BusinessError(
            E_INVALID_CONFIG,
            "clarification_threshold must be within [0, 1]",
            node="config",
        )

    versions = config.get("extractor_versions")
    if not isinstance(versions, dict) or not versions:
        raise BusinessError(
            E_INVALID_CONFIG,
            "extractor_versions must be a non-empty mapping",
            node="config",
        )
    for version, weight in versions.items():
        if not isinstance(version, str) or not version:
            raise BusinessError(
                E_INVALID_CONFIG,
                "extractor version names must be non-empty strings",
                node="config",
            )
        if (
            not isinstance(weight, (int, float))
            or isinstance(weight, bool)
            or float(weight) <= 0
        ):
            raise BusinessError(
                E_INVALID_CONFIG,
                f"extractor weight for {version} must be positive",
                node="config",
            )

    decimals = config.get("score_decimals")
    if (
        not isinstance(decimals, int)
        or isinstance(decimals, bool)
        or not 0 <= decimals <= 12
    ):
        raise BusinessError(
            E_INVALID_CONFIG,
            "score_decimals must be an integer in [0, 12]",
            node="config",
        )

    if config.get("simultaneous_order") != ["retract", "append"]:
        raise BusinessError(
            E_INVALID_CONFIG,
            "simultaneous_order must be ['retract', 'append']",
            node="config",
        )


def config_fingerprint(config: dict[str, Any]) -> str:
    return sha256_json(config)


def detector_versions(config: dict[str, Any]) -> dict[str, float]:
    return {
        str(version): float(weight)
        for version, weight in config["extractor_versions"].items()
    }
