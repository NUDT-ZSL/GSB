from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .errors import PipelineError


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": "1.0",
    "seed": 20261121,
    "image_prefix": "sample",
    "num_images": 4,
    "canvas_shape": [96, 96],
    "num_keypoints": 17,
    "skeletons_per_image": 2,
    "candidates_per_image": 5,
    "occlusion_ratio": 0.08,
    "heatmap_sigma": 1.25,
    "skeleton_sources": ["gt_alpha", "gt_beta", "gt_gamma", "gt_delta"],
    "candidate_sources": ["candidate_alpha", "candidate_beta", "candidate_gamma"],
    "heatmap": {
        "max_peaks_per_channel": 2,
        "min_peak_intensity": 0.01,
        "exact_interpolation": "1d_quadratic",
        "exact_boundary_mode": "offset_zero_then_clip_to_canvas",
        "approx_block_size": 3,
        "approx_quantization_px": 1.5,
        "group_bin_size": 8.0,
        "max_edge_distance_px": 128.0,
    },
    "matching": {
        "metric": "oks",
        "threshold": 0.5,
        "pck_threshold_px": 4.0,
        "exclude_invisible": True,
        "oks_sigma_scale": 0.05,
    },
    "adaptive": {
        "exact_max_images": 4,
        "exact_max_candidates_per_image": 8,
        "exact_max_keypoints": 24,
        "max_candidates_per_image": 64,
        "max_images": 64,
        "max_peak_comparisons_per_image": 3_000_000,
        "memory_budget_mb": 32.0,
        "memory_hard_limit_mb": 128.0,
        "reference_max_candidates_per_image": 12,
        "reference_max_peak_comparisons_per_image": 3_000_000,
        "reference_memory_mb": 128.0,
        "approx_chunk_size": 2,
        "path_tolerance_px": 3.5,
        "resource_policy": "downgrade",
        "priority": ["memory_safety", "progress", "precision"],
        "exact_match_strategy": "hungarian",
        "approx_match_strategy": "chunked_greedy",
        "fine_grouping_strategy": "fine_graph",
        "approx_grouping_strategy": "coarse_bins",
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
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PipelineError(
            "E_CONFIG_INVALID",
            "config_path",
            f"configuration file not found: {config_path}",
        ) from exc
    except json.JSONDecodeError as exc:
        raise PipelineError(
            "E_CONFIG_INVALID",
            "config_path",
            f"invalid JSON in {config_path}: {exc}",
        ) from exc
    if not isinstance(raw, dict):
        raise PipelineError("E_CONFIG_INVALID", "config", "configuration root must be an object")
    config = _deep_merge(DEFAULT_CONFIG, raw)
    validate_config(config)
    return config


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _expand_per_image(value: Any, count: int, field: str) -> list[Any]:
    if isinstance(value, list):
        if len(value) != count:
            raise PipelineError(
                "E_CONFIG_INVALID",
                field,
                f"{field} must contain exactly {count} values",
            )
        return value
    return [value for _ in range(count)]


def expand_per_image(config: dict[str, Any], field: str) -> list[Any]:
    count = config["num_images"]
    value = config.get(field)
    return _expand_per_image(value, count, field)


def validate_config(config: dict[str, Any]) -> None:
    if not isinstance(config.get("seed"), int) or isinstance(config.get("seed"), bool):
        raise PipelineError("E_CONFIG_INVALID", "seed", "seed must be an integer")
    sigma = config.get("heatmap_sigma")
    if not isinstance(sigma, (int, float)) or isinstance(sigma, bool) or float(sigma) <= 0:
        raise PipelineError(
            "E_CONFIG_INVALID",
            "heatmap_sigma",
            "heatmap_sigma must be positive",
        )
    for field in ("skeleton_sources", "candidate_sources"):
        sources = config.get(field)
        if (
            not isinstance(sources, list)
            or not sources
            or any(not isinstance(source, str) or not source for source in sources)
        ):
            raise PipelineError(
                "E_CONFIG_INVALID",
                field,
                f"{field} must be a non-empty list of non-empty strings",
            )
    if not _is_positive_int(config.get("num_images")):
        raise PipelineError("E_CONFIG_INVALID", "num_images", "num_images must be a positive integer")

    shape = config.get("canvas_shape")
    if (
        not isinstance(shape, list)
        or len(shape) != 2
        or any(not _is_positive_int(value) for value in shape)
        or shape[0] < 4
        or shape[1] < 4
    ):
        raise PipelineError(
            "E_CANVAS_INVALID",
            "canvas_shape",
            "canvas_shape must be [height, width] with integers greater than or equal to 4",
        )

    num_images = config["num_images"]
    keypoint_values = _expand_per_image(config.get("num_keypoints"), num_images, "num_keypoints")
    for index, value in enumerate(keypoint_values):
        if not _is_positive_int(value):
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"num_keypoints[{index}]",
                "num_keypoints must be a positive integer",
            )

    skeleton_values = _expand_per_image(
        config.get("skeletons_per_image"), num_images, "skeletons_per_image"
    )
    candidate_values = _expand_per_image(
        config.get("candidates_per_image"), num_images, "candidates_per_image"
    )
    occlusion_values = _expand_per_image(
        config.get("occlusion_ratio"), num_images, "occlusion_ratio"
    )
    for field, values in (
        ("skeletons_per_image", skeleton_values),
        ("candidates_per_image", candidate_values),
    ):
        for index, value in enumerate(values):
            if not _is_positive_int(value):
                raise PipelineError(
                    "E_CONFIG_INVALID",
                    f"{field}[{index}]",
                    f"{field} values must be positive integers",
                )
    for index, value in enumerate(occlusion_values):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0.0 <= float(value) <= 1.0:
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"occlusion_ratio[{index}]",
                "occlusion_ratio must be in [0, 1]",
            )

    matching = config.get("matching", {})
    threshold = matching.get("threshold")
    if (
        not isinstance(threshold, (int, float))
        or isinstance(threshold, bool)
        or not 0.0 < float(threshold) <= 1.0
    ):
        raise PipelineError(
            "E_THRESHOLD_INVALID",
            "matching.threshold",
            "matching.threshold must be in (0, 1]",
        )
    if matching.get("metric") not in ("oks", "pck"):
        raise PipelineError(
            "E_CONFIG_INVALID",
            "matching.metric",
            "matching.metric must be 'oks' or 'pck'",
        )
    pck_threshold = matching.get("pck_threshold_px")
    if (
        not isinstance(pck_threshold, (int, float))
        or isinstance(pck_threshold, bool)
        or float(pck_threshold) <= 0
    ):
        raise PipelineError(
            "E_CONFIG_INVALID",
            "matching.pck_threshold_px",
            "matching.pck_threshold_px must be positive",
        )

    heatmap = config.get("heatmap", {})
    if heatmap.get("exact_interpolation") != "1d_quadratic":
        raise PipelineError(
            "E_CONFIG_INVALID",
            "heatmap.exact_interpolation",
            "only 1d_quadratic interpolation is supported",
        )
    if heatmap.get("exact_boundary_mode") != "offset_zero_then_clip_to_canvas":
        raise PipelineError(
            "E_CONFIG_INVALID",
            "heatmap.exact_boundary_mode",
            "unsupported boundary mode",
        )
    if not _is_positive_int(heatmap.get("approx_block_size")):
        raise PipelineError(
            "E_CONFIG_INVALID",
            "heatmap.approx_block_size",
            "approx_block_size must be a positive integer",
        )
    if not _is_positive_int(heatmap.get("max_peaks_per_channel")):
        raise PipelineError(
            "E_CONFIG_INVALID",
            "heatmap.max_peaks_per_channel",
            "max_peaks_per_channel must be positive",
        )
    min_peak_intensity = heatmap.get("min_peak_intensity")
    if (
        not isinstance(min_peak_intensity, (int, float))
        or isinstance(min_peak_intensity, bool)
        or float(min_peak_intensity) < 0
    ):
        raise PipelineError(
            "E_CONFIG_INVALID",
            "heatmap.min_peak_intensity",
            "min_peak_intensity must be non-negative",
        )
    for field in (
        "approx_quantization_px",
        "group_bin_size",
        "max_edge_distance_px",
    ):
        value = heatmap.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) <= 0:
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"heatmap.{field}",
                f"{field} must be positive",
            )

    adaptive = config.get("adaptive", {})
    for field in (
        "exact_max_images",
        "exact_max_candidates_per_image",
        "exact_max_keypoints",
        "max_candidates_per_image",
        "max_images",
        "max_peak_comparisons_per_image",
        "reference_max_candidates_per_image",
        "reference_max_peak_comparisons_per_image",
        "approx_chunk_size",
    ):
        if not _is_positive_int(adaptive.get(field)):
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"adaptive.{field}",
                f"{field} must be a positive integer",
            )
    for field in (
        "memory_budget_mb",
        "memory_hard_limit_mb",
        "reference_memory_mb",
        "path_tolerance_px",
    ):
        value = adaptive.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) <= 0:
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"adaptive.{field}",
                f"{field} must be positive",
            )
    if adaptive.get("resource_policy") not in ("downgrade", "error"):
        raise PipelineError(
            "E_CONFIG_INVALID",
            "adaptive.resource_policy",
            "resource_policy must be 'downgrade' or 'error'",
        )
    if not isinstance(adaptive.get("priority"), list) or not adaptive["priority"]:
        raise PipelineError(
            "E_CONFIG_INVALID",
            "adaptive.priority",
            "priority must be a non-empty list",
        )
