"""Configuration loading, validation, and stable fingerprints."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "generator": {
        "token_length": 32,
        "sample_count": 32,
        "material_families": ["oxide", "alloy", "polymer", "rare_earth", "halide"],
        "material_weights": {
            "oxide": 0.32,
            "alloy": 0.28,
            "polymer": 0.24,
            "rare_earth": 0.10,
            "halide": 0.06,
        },
        "rare_material_families": ["rare_earth", "halide"],
        "instrument_sources": [
            "Raman-Lab-01",
            "Raman-Lab-02",
            "XRD-Beamline-07",
            "XRD-Lab-03",
        ],
        "missing_probability": 0.7,
        "missing_block_size_min": 2,
        "missing_block_size_max": 4,
        "max_missing_blocks": 2,
        "noise_std": 0.012,
        "grid": {
            "wavenumber_start_cm-1": 400.0,
            "wavenumber_stop_cm-1": 1800.0,
            "angle_start_deg": 8.0,
            "angle_stop_deg": 88.0,
        },
        "version_intensity_scale": {"A": 1.0, "B": 4.0, "C": 16.0},
    },
    "model": {
        "num_experts": 4,
        "top_k": 2,
        "token_feature_dim": 16,
        "initial_temperature": 1.0,
        "learning_rate": 0.008,
        "weight_decay": 0.0001,
        "batch_size": 8,
        "epochs": 8,
        "capacity_factor": 1.1,
        "overflow_strategy": "reroute",
        "balance_weight": 0.08,
        "route_weight": 0.002,
        "capacity_weight": 0.05,
        "near_tie_margin": 0.015,
        "gate_bias_init": 0.0,
        "expert_identity_scale": 0.65,
    },
    "collapse": {
        "utilization_threshold": 0.08,
        "patience_steps": 3,
        "adjustment_strategy": "temperature",
        "temperature_multiplier": 1.45,
        "max_temperature": 4.0,
        "max_adjustments": 2,
        "cooldown_steps": 2,
        "reinit_seed_offset": 1000,
    },
    "evaluation": {
        "batch_size": 8,
        "concentration_threshold": 0.8,
        "value_tolerance": 1e-10,
        "loss_tolerance": 1e-12,
    },
    "output": {
        "save_raw_data": True,
        "save_assignments": True,
        "save_adjustment_assignments": True,
        "float_decimals": 12,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        supplied = json.load(handle)
    config = _deep_merge(DEFAULT_CONFIG, supplied)
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    generator = config["generator"]
    model = config["model"]
    collapse = config["collapse"]
    evaluation = config["evaluation"]

    families = list(generator["material_families"])
    if not families:
        raise ValueError("generator.material_families cannot be empty")
    missing_weights = set(families) - set(generator["material_weights"])
    if missing_weights:
        raise ValueError(
            "missing material weights for: " + ", ".join(sorted(missing_weights))
        )
    rare = set(generator["rare_material_families"])
    if not rare.issubset(families):
        raise ValueError("rare_material_families must be a subset of material_families")
    if not generator["instrument_sources"]:
        raise ValueError("generator.instrument_sources cannot be empty")
    if int(generator["token_length"]) < 4:
        raise ValueError("generator.token_length must be at least 4")
    if int(generator["sample_count"]) < 4:
        raise ValueError("generator.sample_count must be at least 4")
    if not 0.0 <= float(generator["missing_probability"]) <= 1.0:
        raise ValueError("generator.missing_probability must be in [0, 1]")
    if int(generator["missing_block_size_min"]) < 1:
        raise ValueError("generator.missing_block_size_min must be positive")
    if int(generator["missing_block_size_max"]) < int(
        generator["missing_block_size_min"]
    ):
        raise ValueError(
            "generator.missing_block_size_max must be >= missing_block_size_min"
        )
    if int(generator["max_missing_blocks"]) < 0:
        raise ValueError("generator.max_missing_blocks cannot be negative")
    for version in ("A", "B", "C"):
        if float(generator["version_intensity_scale"][version]) <= 0.0:
            raise ValueError(
                f"generator.version_intensity_scale.{version} must be positive"
            )

    token_length = int(generator["token_length"])
    num_experts = int(model["num_experts"])
    top_k = int(model["top_k"])
    token_feature_dim = int(model["token_feature_dim"])
    if num_experts < 2:
        raise ValueError("model.num_experts must be at least 2")
    if not 1 <= top_k <= num_experts:
        raise ValueError("model.top_k must be in [1, num_experts]")
    if token_feature_dim < 4:
        raise ValueError("model.token_feature_dim must be at least 4")
    if int(model["batch_size"]) < 1:
        raise ValueError("model.batch_size must be positive")
    if int(model["epochs"]) < 1:
        raise ValueError("model.epochs must be positive")
    if float(model["capacity_factor"]) <= 0.0:
        raise ValueError("model.capacity_factor must be positive")
    if model["overflow_strategy"] not in {"drop", "reroute"}:
        raise ValueError("model.overflow_strategy must be 'drop' or 'reroute'")
    if float(model["learning_rate"]) <= 0.0:
        raise ValueError("model.learning_rate must be positive")
    if float(model["initial_temperature"]) <= 0.0:
        raise ValueError("model.initial_temperature must be positive")
    bias_init = model["gate_bias_init"]
    if isinstance(bias_init, (list, tuple)):
        if len(bias_init) != num_experts:
            raise ValueError(
                "model.gate_bias_init list length must equal model.num_experts"
            )
        if not all(_is_finite_number(value) for value in bias_init):
            raise ValueError("model.gate_bias_init values must be finite numbers")
    elif not _is_finite_number(bias_init):
        raise ValueError("model.gate_bias_init must be a finite number or list")
    if int(evaluation["batch_size"]) < 1:
        raise ValueError("evaluation.batch_size must be positive")
    if not 0.0 <= float(evaluation["concentration_threshold"]) <= 1.0:
        raise ValueError("evaluation.concentration_threshold must be in [0, 1]")

    if collapse["adjustment_strategy"] not in {
        "temperature",
        "reinitialize_gate",
        "none",
    }:
        raise ValueError(
            "collapse.adjustment_strategy must be temperature, reinitialize_gate, or none"
        )
    if int(collapse["patience_steps"]) < 1:
        raise ValueError("collapse.patience_steps must be positive")
    if int(collapse["max_adjustments"]) < 0:
        raise ValueError("collapse.max_adjustments cannot be negative")
    if int(collapse["cooldown_steps"]) < 0:
        raise ValueError("collapse.cooldown_steps cannot be negative")
    if float(collapse["temperature_multiplier"]) <= 0.0:
        raise ValueError("collapse.temperature_multiplier must be positive")

    # Keep a single canonical token width across generation, routing, and reports.
    if config["output"].get("float_decimals", 12) < 0:
        raise ValueError("output.float_decimals cannot be negative")
    config["_validated_token_length"] = token_length


def stable_config_json(config: dict[str, Any]) -> str:
    public = {key: value for key, value in config.items() if not key.startswith("_")}
    return json.dumps(public, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def config_fingerprint(config: dict[str, Any]) -> str:
    return hashlib.sha256(stable_config_json(config).encode("utf-8")).hexdigest()


def _is_finite_number(value: Any) -> bool:
    try:
        return bool(math.isfinite(float(value)))
    except (TypeError, ValueError):
        return False
