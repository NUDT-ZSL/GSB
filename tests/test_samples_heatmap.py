from __future__ import annotations

import copy
import json

import numpy as np
import pytest

from pose_pipeline.errors import PipelineError
from pose_pipeline.heatmap import decode_approx, decode_exact
from pose_pipeline.samples import (
    generate_sample,
    load_manifest,
    synthesize_heatmaps,
    verify_saved_heatmaps,
)
from tests.helpers import make_config, make_dataset


def test_sample_and_heatmap_generation_is_deterministic(tmp_path):
    config = make_config(num_images=1)
    first = generate_sample(config, 0)
    second = generate_sample(config, 0)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    first_heatmaps = synthesize_heatmaps(first, config["heatmap_sigma"])
    second_heatmaps = synthesize_heatmaps(second, config["heatmap_sigma"])
    assert np.array_equal(first_heatmaps, second_heatmaps)
    assert first["heatmap_sha256"] == second["heatmap_sha256"]


def test_saved_heatmap_round_trip_and_tamper_detection(tmp_path):
    config = make_config(num_images=1)
    samples, manifest_dir = make_dataset(tmp_path, config)
    rebuilt = verify_saved_heatmaps(samples[0], manifest_dir, config["heatmap_sigma"])
    assert rebuilt.shape == (2, samples[0]["num_channels"], 32, 32)
    heatmap_path = manifest_dir / samples[0]["heatmap_file"]
    stored = np.load(heatmap_path)
    stored[0, 0, 0, 0] += 1.0
    np.save(heatmap_path, stored)
    with pytest.raises(PipelineError) as exc_info:
        verify_saved_heatmaps(samples[0], manifest_dir, config["heatmap_sigma"])
    assert exc_info.value.code == "E_INCONSISTENT"
    assert exc_info.value.category == "HEATMAP_FILE_HASH"


def _single_keypoint_sample(canvas_shape: list[int], x: float, y: float):
    config = make_config(
        num_images=1,
        num_keypoints=1,
        canvas_shape=canvas_shape,
    )
    sample = generate_sample(config, 0)
    sample["skeletons"][0]["keypoints"][0]["x"] = x
    sample["skeletons"][0]["keypoints"][0]["y"] = y
    sample["skeletons"][0]["keypoints"][0]["confidence"] = 1.0
    sample["skeletons"][0]["keypoints"][0]["visible"] = True
    return sample


def _gaussian_heatmaps(sample, center_x: float, center_y: float, sigma: float = 1.2):
    height, width = sample["canvas_shape"]
    ys, xs = np.mgrid[0:height, 0:width]
    target = np.exp(
        -((xs - center_x) ** 2 + (ys - center_y) ** 2) / (2.0 * sigma**2)
    ).astype(np.float32)
    return np.stack([target.copy(), target], axis=0)[:, None, :, :]


def test_exact_subpixel_offset_and_approx_quantization():
    sample = _single_keypoint_sample([12, 12], 4.0, 4.0)
    config = make_config(num_keypoints=1, canvas_shape=[12, 12])
    heatmaps = _gaussian_heatmaps(sample, 5.25, 4.0)
    exact = decode_exact(heatmaps, sample, config["heatmap"])
    approx = decode_approx(heatmaps, sample, config["heatmap"])
    exact_peak = exact["peaks"][0]
    approx_peak = approx["peaks"][0]
    assert exact_peak["x_pixel"] == 5
    assert exact_peak["subpixel_offset"]["dx"] > 0
    assert exact_peak["x"] == pytest.approx(5.25, abs=0.08)
    assert approx_peak["x"] == 4.0
    assert approx_peak["subpixel_offset"] == {"dx": 0.0, "dy": 0.0}
    assert exact["comparison_count"] > approx["comparison_count"]


def test_exact_boundary_offset_is_zero_and_coordinate_is_clipped():
    sample = _single_keypoint_sample([10, 10], 0.0, 5.0)
    config = make_config(num_keypoints=1, canvas_shape=[10, 10])
    height, width = sample["canvas_shape"]
    ys, xs = np.mgrid[0:height, 0:width]
    target = np.exp(-((xs - 0.0) ** 2 + (ys - 5.0) ** 2) / (2.0 * 1.2**2)).astype(
        np.float32
    )
    heatmaps = np.stack([target, target])[:, None, :, :]
    decoded = decode_exact(heatmaps, sample, config["heatmap"])
    peak = decoded["peaks"][0]
    assert peak["x_pixel"] == 0
    assert peak["subpixel_offset"]["dx"] == 0.0
    assert peak["x"] == 0.0


def test_peak_tie_break_uses_keypoint_index_then_pixel_coordinates():
    config = make_config(num_images=1, num_keypoints=2, canvas_shape=[8, 8])
    sample = generate_sample(config, 0)
    target = np.zeros((2, 8, 8), dtype=np.float32)
    target[0, 2, 3] = 1.0
    target[1, 1, 2] = 1.0
    heatmaps = np.stack([target, target])
    decoded = decode_exact(heatmaps, sample, config["heatmap"])
    assert [(peak["keypoint_index"], peak["x"], peak["y"]) for peak in decoded["peaks"]] == [
        (0, 3.0, 2.0),
        (1, 2.0, 1.0),
    ]


def test_manifest_round_trip_keeps_required_fields(tmp_path):
    config = make_config(num_images=2, candidates=[2, 4])
    make_dataset(tmp_path, config)
    samples, _ = load_manifest(tmp_path / "data" / "manifest.jsonl")
    required = {
        "image_id",
        "canvas_shape",
        "num_keypoints",
        "keypoint_meta",
        "heatmap_seed",
        "occlusion_ratio",
    }
    assert required <= set(samples[0])
    assert len(samples[0]["keypoint_meta"]) == samples[0]["num_keypoints"]
