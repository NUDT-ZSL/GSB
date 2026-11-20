from __future__ import annotations

import math
from typing import Any

from .errors import PipelineError
from .utils import round_float


def peak_comparison_estimates(
    sample: dict[str, Any], heatmap_config: dict[str, Any]
) -> dict[str, int]:
    height, width = sample["canvas_shape"]
    channels = sample["num_channels"]
    block_size = int(heatmap_config["approx_block_size"])
    coarse_height = int(math.ceil(height / block_size))
    coarse_width = int(math.ceil(width / block_size))
    max_peaks = int(heatmap_config["max_peaks_per_channel"])
    exact = channels * height * width * 8 + channels * max_peaks * 4
    approx = (
        channels * height * width
        + channels * coarse_height * coarse_width * 8
        + channels * max_peaks * 4
    )
    return {
        "exact": int(exact),
        "approx": int(approx),
    }


def memory_estimates(
    sample: dict[str, Any],
    heatmap_config: dict[str, Any],
    adaptive_config: dict[str, Any],
) -> dict[str, int]:
    height, width = sample["canvas_shape"]
    channels = sample["num_channels"]
    block_size = int(heatmap_config["approx_block_size"])
    coarse_height = int(math.ceil(height / block_size))
    coarse_width = int(math.ceil(width / block_size))
    max_peaks = int(heatmap_config["max_peaks_per_channel"])
    skeletons = len(sample["skeletons"])
    candidates = len(sample["candidates"])
    heatmap_bytes = channels * height * width * 4 * 2
    exact_graph_bytes = channels * max(0, channels - 1) // 2 * 16
    approximate_graph_bytes = channels * 64
    peak_bytes = channels * max_peaks * 192
    exact_match_bytes = (
        skeletons * candidates + skeletons * skeletons
    ) * 8 * 4
    chunk_size = int(adaptive_config["approx_chunk_size"])
    approx_match_bytes = (
        min(skeletons, chunk_size) * candidates
        + min(skeletons, chunk_size) * skeletons
    ) * 8 * 4
    exact = (
        heatmap_bytes
        + exact_graph_bytes
        + peak_bytes
        + exact_match_bytes
        + channels * height * width * 4
    )
    approx = (
        heatmap_bytes
        + coarse_height * coarse_width * channels * 4
        + approximate_graph_bytes
        + peak_bytes
        + approx_match_bytes
    )
    return {
        "exact": int(exact),
        "approx": int(approx),
    }


def _mb(value_bytes: int) -> float:
    return round_float(value_bytes / (1024.0 * 1024.0))


def observation_for(sample: dict[str, Any], num_images: int) -> dict[str, Any]:
    return {
        "num_images": num_images,
        "candidates_per_image": len(sample["candidates"]),
        "num_keypoints": sample["num_keypoints"],
        "skeleton_count": len(sample["skeletons"]),
        "heatmap_channels": sample["num_channels"],
        "canvas_pixels": int(sample["canvas_shape"][0] * sample["canvas_shape"][1]),
    }


def exact_reference_availability(
    sample: dict[str, Any],
    comparison_estimates: dict[str, int],
    memory_estimates_bytes: dict[str, int],
    adaptive_config: dict[str, Any],
) -> dict[str, Any]:
    reasons = []
    if len(sample["candidates"]) > int(
        adaptive_config["reference_max_candidates_per_image"]
    ):
        reasons.append(
            {
                "code": "REFERENCE_CANDIDATE_LIMIT",
                "observed": len(sample["candidates"]),
                "limit": int(adaptive_config["reference_max_candidates_per_image"]),
            }
        )
    if comparison_estimates["exact"] > int(
        adaptive_config["reference_max_peak_comparisons_per_image"]
    ):
        reasons.append(
            {
                "code": "REFERENCE_PEAK_COMPARISON_LIMIT",
                "observed": comparison_estimates["exact"],
                "limit": int(
                    adaptive_config["reference_max_peak_comparisons_per_image"]
                ),
            }
        )
    reference_memory_bytes = int(
        float(adaptive_config["reference_memory_mb"]) * 1024.0 * 1024.0
    )
    if memory_estimates_bytes["exact"] > reference_memory_bytes:
        reasons.append(
            {
                "code": "REFERENCE_MEMORY_LIMIT",
                "observed_mb": _mb(memory_estimates_bytes["exact"]),
                "limit_mb": round_float(adaptive_config["reference_memory_mb"]),
            }
        )
    return {
        "available": not reasons,
        "reasons": reasons,
    }


def decide_path(
    sample: dict[str, Any],
    num_images: int,
    config: dict[str, Any],
) -> dict[str, Any]:
    adaptive = config["adaptive"]
    heatmap_config = config["heatmap"]
    observations = observation_for(sample, num_images)
    comparison_estimates = peak_comparison_estimates(sample, heatmap_config)
    memory_estimates_bytes = memory_estimates(sample, heatmap_config, adaptive)
    exact_memory_mb = _mb(memory_estimates_bytes["exact"])
    approx_memory_mb = _mb(memory_estimates_bytes["approx"])
    max_peak_comparisons = int(adaptive["max_peak_comparisons_per_image"])
    hard_memory_mb = float(adaptive["memory_hard_limit_mb"])

    hard_reasons = []
    if num_images > int(adaptive["max_images"]):
        hard_reasons.append(
            {
                "code": "IMAGE_LIMIT",
                "observed": num_images,
                "limit": int(adaptive["max_images"]),
            }
        )
    if len(sample["candidates"]) > int(adaptive["max_candidates_per_image"]):
        hard_reasons.append(
            {
                "code": "CANDIDATE_LIMIT",
                "observed": len(sample["candidates"]),
                "limit": int(adaptive["max_candidates_per_image"]),
            }
        )
    if comparison_estimates["approx"] > max_peak_comparisons:
        hard_reasons.append(
            {
                "code": "APPROX_PEAK_COMPARISON_LIMIT",
                "observed": comparison_estimates["approx"],
                "limit": max_peak_comparisons,
            }
        )
    if approx_memory_mb > hard_memory_mb:
        hard_reasons.append(
            {
                "code": "APPROX_HARD_MEMORY_LIMIT",
                "observed_mb": approx_memory_mb,
                "limit_mb": round_float(hard_memory_mb),
            }
        )
    if hard_reasons:
        raise PipelineError(
            "E_RESOURCE_LIMIT",
            "adaptive",
            "sample exceeds a hard resource limit and cannot be processed without dropping work",
            image_id=sample["image_id"],
            details={"violations": hard_reasons},
        )

    exact_reasons = []
    if num_images > int(adaptive["exact_max_images"]):
        exact_reasons.append(
            {
                "code": "IMAGE_COUNT",
                "observed": num_images,
                "limit": int(adaptive["exact_max_images"]),
            }
        )
    if len(sample["candidates"]) > int(adaptive["exact_max_candidates_per_image"]):
        exact_reasons.append(
            {
                "code": "CANDIDATE_COUNT",
                "observed": len(sample["candidates"]),
                "limit": int(adaptive["exact_max_candidates_per_image"]),
            }
        )
    if sample["num_keypoints"] > int(adaptive["exact_max_keypoints"]):
        exact_reasons.append(
            {
                "code": "KEYPOINT_COUNT",
                "observed": sample["num_keypoints"],
                "limit": int(adaptive["exact_max_keypoints"]),
            }
        )
    if comparison_estimates["exact"] > max_peak_comparisons:
        exact_reasons.append(
            {
                "code": "PEAK_COMPARISON_BUDGET",
                "observed": comparison_estimates["exact"],
                "limit": max_peak_comparisons,
            }
        )
    if exact_memory_mb > float(adaptive["memory_budget_mb"]):
        exact_reasons.append(
            {
                "code": "MEMORY_SOFT_BUDGET",
                "observed_mb": exact_memory_mb,
                "limit_mb": round_float(adaptive["memory_budget_mb"]),
            }
        )

    if not exact_reasons:
        selected_path = "exact"
        degraded = False
    else:
        if adaptive["resource_policy"] == "error":
            raise PipelineError(
                "E_RESOURCE_LIMIT",
                "adaptive.resource_policy",
                "exact path is not feasible and resource_policy forbids downgrade",
                image_id=sample["image_id"],
                details={"violations": exact_reasons},
            )
        selected_path = "approx"
        degraded = True

    reference = exact_reference_availability(
        sample,
        comparison_estimates,
        memory_estimates_bytes,
        adaptive,
    )
    selected_comparisons = comparison_estimates[selected_path]
    selected_memory = (
        exact_memory_mb if selected_path == "exact" else approx_memory_mb
    )
    degradation_categories = []
    if degraded:
        degradation_categories = [
            "PEAK_RESOLUTION",
            "MATCHING_STRATEGY",
            "GROUPING_RESOLUTION",
        ]
        if any(
            reason["code"] in ("IMAGE_COUNT", "CANDIDATE_COUNT", "KEYPOINT_COUNT")
            for reason in exact_reasons
        ):
            degradation_categories.append("SCALE_DOWNGRADE")
        if any(
            reason["code"].endswith("BUDGET") for reason in exact_reasons
        ):
            degradation_categories.append("RESOURCE_DOWNGRADE")

    return {
        "image_id": sample["image_id"],
        "observations": observations,
        "estimates": {
            "peak_comparisons": comparison_estimates,
            "memory_mb": {
                "exact": exact_memory_mb,
                "approx": approx_memory_mb,
            },
            "matching_matrix_cells": {
                "exact_resident": len(sample["skeletons"])
                * (len(sample["skeletons"]) + len(sample["candidates"])),
                "approx_resident": int(adaptive["approx_chunk_size"])
                * (
                    len(sample["skeletons"]) + len(sample["candidates"])
                ),
            },
        },
        "limits": {
            "max_peak_comparisons_per_image": max_peak_comparisons,
            "memory_budget_mb": round_float(adaptive["memory_budget_mb"]),
            "memory_hard_limit_mb": round_float(hard_memory_mb),
            "exact_max_images": int(adaptive["exact_max_images"]),
            "exact_max_candidates_per_image": int(
                adaptive["exact_max_candidates_per_image"]
            ),
            "exact_max_keypoints": int(adaptive["exact_max_keypoints"]),
        },
        "priority": list(adaptive["priority"]),
        "selected_path": selected_path,
        "degraded": degraded,
        "degradation_categories": degradation_categories,
        "reasons": exact_reasons,
        "selected_peak_comparisons": int(selected_comparisons),
        "selected_memory_mb": selected_memory,
        "selected_memory_exceeds_soft_budget": selected_memory
        > float(adaptive["memory_budget_mb"]),
        "coverage_complete": True,
        "exact_reference": reference,
    }
