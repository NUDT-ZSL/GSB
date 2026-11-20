from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from pose_pipeline.config import DEFAULT_CONFIG, validate_config
from pose_pipeline.samples import generate_dataset, load_manifest


def make_config(
    *,
    num_images: int = 1,
    candidates: int | list[int] = 3,
    skeletons: int | list[int] = 1,
    num_keypoints: int | list[int] = 5,
    canvas_shape: list[int] | None = None,
) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["seed"] = 99173
    config["image_prefix"] = "test"
    config["num_images"] = num_images
    config["canvas_shape"] = canvas_shape or [32, 32]
    config["num_keypoints"] = num_keypoints
    config["skeletons_per_image"] = skeletons
    config["candidates_per_image"] = candidates
    config["occlusion_ratio"] = 0.1
    config["matching"]["pck_threshold_px"] = 1.5
    config["adaptive"]["exact_max_images"] = 8
    config["adaptive"]["exact_max_candidates_per_image"] = 12
    config["adaptive"]["exact_max_keypoints"] = 24
    config["adaptive"]["max_candidates_per_image"] = 64
    config["adaptive"]["max_peak_comparisons_per_image"] = 1_000_000
    config["adaptive"]["reference_max_candidates_per_image"] = 12
    config["adaptive"]["reference_max_peak_comparisons_per_image"] = 1_000_000
    validate_config(config)
    return config


def make_dataset(
    tmp_path: Path, config: dict[str, Any]
) -> tuple[list[dict[str, Any]], Path]:
    manifest = generate_dataset(config, tmp_path / "data")
    return load_manifest(manifest)
