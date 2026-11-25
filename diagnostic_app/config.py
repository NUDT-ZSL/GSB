from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .errors import AppError, ErrorCode
from .hashing import sha256_json


DEFAULT_CONFIG: dict[str, Any] = {
    "config_version": "diagnostic-config-v1",
    "pipeline_version": "det-cond-v1",
    "condition": {
        "dim": 16,
        "vocab_seed": 917263,
        "embedding_scale": 0.35,
        "normalize": "weighted_mean_l2",
    },
    "fusion": {
        "image_weight": 0.68,
        "condition_weight": 0.32,
        "bias": 0.0,
        "normalization": "sum_to_one_v1",
        "condition_gain": 1.15,
    },
    "iteration": {
        "step_size": 0.22,
        "coupling_strength": 0.42,
        "coupling_radius": 1,
        "condition_gain": 0.10,
        "boundary": "edge_clamp",
        "update_order": "row_major_jacobi_double_buffer",
        "aggregation": "mean_of_iterates_v1",
        "aggregate_initial": False,
    },
    "grid": {
        "rows": "auto",
        "cols": "auto",
        "target_block_pixels": 400,
        "max_block_pixels": 512,
        "min_block_pixels": 36,
        "max_grid_side": 8,
        "candidates": [[1, 1], [2, 2], [2, 3], [3, 3], [4, 4], [6, 6]],
        "center_first": True,
    },
    "cost_model": {
        "fixed_overhead_ms": 0.08,
        "ms_per_value_step_tap": 0.000075,
        "halo_cost_factor": 1.15,
    },
    "latency_budget_ms": 25.0,
    "latency_budget_scope": "first_block",
    "resolution_policy": {
        "priority_order": ["latency_budget", "consistency_strength", "block_partition"],
        "latency_first_rule": "coarsest_grid_whose_estimate_is_within_budget",
        "consistency_check": "bytewise",
        "allow_finer_downgrade": True,
    },
    "anchor": {
        "strength_min": 0.0,
        "strength_max": 1.0,
        "default_strength": 0.18,
        "initial_mix": 0.35,
        "statistic": "channel_mean_std_and_local_pixels_v1",
    },
    "resource_limits": {
        "max_total_pixels": 1_048_576,
        "max_block_memory_bytes": 4_194_304,
        "max_parallel_tasks": 4,
        "block_buffer_copies": 5,
        "allow_smaller_block_downgrade": True,
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    if path is not None:
        source = Path(path)
        loaded = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise AppError(ErrorCode.INVALID_REQUEST, "configuration root must be an object")
        config = deep_merge(config, loaded)
    if overrides:
        config = deep_merge(config, overrides)
    return config


def config_checksum(config: dict[str, Any]) -> str:
    return sha256_json(config)


def precision_dtype(precision_profile: str):
    import numpy as np

    profile = (precision_profile or "float32").lower()
    if "float64" in profile or "fp64" in profile or "double" in profile or "high" in profile:
        return np.float64
    if "float32" in profile or "fp32" in profile or "standard" in profile or "balanced" in profile:
        return np.float32
    raise AppError(ErrorCode.INVALID_REQUEST, f"unsupported precision_profile: {precision_profile}")


def validate_task(task: dict[str, Any]) -> None:
    required = ["task_id", "image_seed", "image_shape", "text", "prompt",
                "sampler_steps", "noise_scale", "precision_profile"]
    missing = [key for key in required if key not in task]
    if missing:
        raise AppError(ErrorCode.INVALID_REQUEST, "task is missing required fields", {"missing": missing})
    shape = task["image_shape"]
    if not (isinstance(shape, list) and len(shape) == 3 and all(isinstance(v, int) and v > 0 for v in shape)):
        raise AppError(ErrorCode.INVALID_REQUEST, "image_shape must be [height, width, channels]")
    if shape[2] not in (1, 3):
        raise AppError(ErrorCode.INVALID_REQUEST, "only grayscale or RGB canvases are supported")
    if int(task["sampler_steps"]) < 1:
        raise AppError(ErrorCode.INVALID_REQUEST, "sampler_steps must be >= 1")
