from __future__ import annotations

from typing import Any

from .jsonutil import stable_hash


def window_rule(metrics_config: dict[str, Any], origin_ms: int) -> dict[str, Any]:
    first = next(iter(metrics_config["metrics"].values()))
    return {
        "origin_logical_ms": origin_ms,
        "window_length_ms": first["window_length_ms"],
        "window_step_ms": first["window_step_ms"],
        "ownership_policy": "aligned_step_unique_owner",
    }


def window_version(metrics_config: dict[str, Any], origin_ms: int) -> str:
    return "win-" + stable_hash(window_rule(metrics_config, origin_ms))


def metric_version(metric_definition: dict[str, Any]) -> str:
    return "met-" + stable_hash(metric_definition)


def threshold_version(unified_view: dict[str, Any]) -> str:
    material = [
        {
            "scope": entry["scope"],
            "metric": entry["metric"],
            "level": entry["level"],
            "value": entry["value"],
            "effective_logical_ms": entry["effective_logical_ms"],
            "generation": entry["generation"],
            "source_version": entry["source_version"],
        }
        for entry in unified_view.get("entries", [])
    ]
    return "thr-" + stable_hash(material)
