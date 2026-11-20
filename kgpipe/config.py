from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .errors import KGError
from .util import attach_checksum, read_json, verify_checksum


DEFAULT_CONFIG: dict[str, Any] = {
    "config_version": 1,
    "pair_budget": 12000,
    "memory_limit_mb": 128,
    "reserved_memory_mb": 16,
    "bytes_per_pair": 256,
    "approx_neighbors": 4,
    "mandatory_confidence_bp": 9000,
    "budget_policy": "error",
    "shard_overhead_units": 8,
    "exact_entity_soft_limit": 24,
}


def load_config(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        result = deepcopy(DEFAULT_CONFIG)
        return attach_checksum(result)
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise KGError(
            "E_CONFIG_INVALID",
            "抽取配置必须是 JSON 对象",
            details={"path": str(path)},
        )
    version = raw.get("config_version")
    if not isinstance(version, int) or version < 1:
        raise KGError(
            "E_CONFIG_INVALID",
            "config_version 必须为正整数",
            details={"config_version": version},
        )
    verify_checksum(raw, category="config", version=version)
    result = deepcopy(DEFAULT_CONFIG)
    result.update(raw)
    return result


def effective_config(
    base: dict[str, Any],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in overrides.items():
        if value is not None:
            result[key] = value
    _validate_config(result)
    result.pop("checksum", None)
    return attach_checksum(result)


def _validate_config(config: dict[str, Any]) -> None:
    positive_keys = (
        "config_version",
        "pair_budget",
        "memory_limit_mb",
        "bytes_per_pair",
        "mandatory_confidence_bp",
        "shard_overhead_units",
        "exact_entity_soft_limit",
    )
    for key in positive_keys:
        value = config.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise KGError(
                "E_CONFIG_INVALID",
                f"配置项 {key} 必须为正整数",
                details={"config_key": key, "value": value},
            )
    reserved = config.get("reserved_memory_mb")
    if not isinstance(reserved, int) or reserved < 0:
        raise KGError(
            "E_CONFIG_INVALID",
            "reserved_memory_mb 必须为非负整数",
            details={"value": reserved},
        )
    if reserved >= config["memory_limit_mb"]:
        raise KGError(
            "E_CONFIG_INVALID",
            "reserved_memory_mb 必须小于 memory_limit_mb",
            details={
                "reserved_memory_mb": reserved,
                "memory_limit_mb": config["memory_limit_mb"],
            },
        )
    neighbors = config.get("approx_neighbors")
    if not isinstance(neighbors, int) or isinstance(neighbors, bool) or neighbors < 0:
        raise KGError(
            "E_CONFIG_INVALID",
            "approx_neighbors 必须为非负整数",
            details={"value": neighbors},
        )
    if config.get("budget_policy") not in {"error", "degrade"}:
        raise KGError(
            "E_CONFIG_INVALID",
            "budget_policy 必须为 error 或 degrade",
            details={"value": config.get("budget_policy")},
        )
    if config["mandatory_confidence_bp"] > 10000:
        raise KGError(
            "E_CONFIG_INVALID",
            "mandatory_confidence_bp 不能超过 10000",
            details={"value": config["mandatory_confidence_bp"]},
        )
