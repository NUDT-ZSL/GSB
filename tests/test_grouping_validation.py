from __future__ import annotations

import copy

import pytest

from pose_pipeline.adaptive import decide_path
from pose_pipeline.config import validate_config
from pose_pipeline.errors import PipelineError
from pose_pipeline.grouping import assemble_instances
from pose_pipeline.samples import generate_sample
from pose_pipeline.validation import validate_samples
from tests.helpers import make_config


def _peak(sample, channel, keypoint_index, x, y, intensity=0.9):
    height = sample["canvas_shape"][0]
    width = sample["canvas_shape"][1]
    assert 0 <= x < width and 0 <= y < height
    skeleton = sample["skeletons"][0]
    return {
        "peak_id": f"{sample['image_id']}:ch{channel:03d}:p0",
        "channel_index": channel,
        "skeleton_id": 0,
        "source": skeleton["source"],
        "keypoint_index": keypoint_index,
        "rank": 0,
        "path": "exact",
        "x_pixel": x,
        "y_pixel": y,
        "x": float(x),
        "y": float(y),
        "intensity": intensity,
        "subpixel_offset": {"dx": 0.0, "dy": 0.0},
        "quantization_px": 0.0,
    }


def test_grouping_builds_connected_instance_with_edge_evidence():
    config = make_config(num_images=1, num_keypoints=3, canvas_shape=[16, 16])
    sample = generate_sample(config, 0)
    sample["skeletons"][0]["edges"] = [[0, 1], [1, 2]]
    peaks = [
        _peak(sample, 0, 0, 3, 3, 0.9),
        _peak(sample, 1, 1, 4, 4, 0.8),
        _peak(sample, 2, 2, 5, 5, 0.7),
    ]
    groups = {(0, sample["skeletons"][0]["source"]): peaks}
    instances = assemble_instances(groups, sample, "exact", config["heatmap"])
    assert len(instances) == 1
    instance = instances[0]
    assert instance["connected"] is True
    assert [point["keypoint_index"] for point in instance["keypoints"]] == [0, 1, 2]
    assert len(instance["edge_evidence"]) == 2
    assert instance["edge_evidence"][0]["intensity_delta"] == pytest.approx(0.1)
    assert instance["edge_evidence"][0]["distance"] == pytest.approx(2**0.5)


def test_grouping_detects_keypoint_conflict():
    config = make_config(num_images=1, num_keypoints=3, canvas_shape=[16, 16])
    sample = generate_sample(config, 0)
    sample["skeletons"][0]["edges"] = [[0, 1], [1, 2]]
    peak = _peak(sample, 0, 0, 3, 3)
    groups = {
        (0, "source_a"): [peak],
        (0, "source_b"): [peak],
    }
    with pytest.raises(PipelineError) as exc_info:
        assemble_instances(groups, sample, "exact", config["heatmap"])
    assert exc_info.value.code == "E_KEYPOINT_CONFLICT"


def test_grouping_detects_disconnected_skeleton():
    config = make_config(num_images=1, num_keypoints=3, canvas_shape=[16, 16])
    sample = generate_sample(config, 0)
    sample["skeletons"][0]["edges"] = [[0, 1], [1, 2]]
    peaks = [
        _peak(sample, 0, 0, 3, 3),
        _peak(sample, 1, 1, 4, 4),
    ]
    with pytest.raises(PipelineError) as exc_info:
        assemble_instances(
            {(0, sample["skeletons"][0]["source"]): peaks},
            sample,
            "exact",
            config["heatmap"],
        )
    assert exc_info.value.code == "E_SKELETON_DISCONNECT"
    assert exc_info.value.details["missing_keypoint_indices"] == [2]


def test_validation_business_codes_are_specific():
    config = make_config(num_images=1, num_keypoints=5, canvas_shape=[32, 32])
    sample = generate_sample(config, 0)
    validate_samples([sample], config)

    with pytest.raises(PipelineError) as exc_info:
        validate_samples([sample, copy.deepcopy(sample)], config)
    assert exc_info.value.code == "E_DUP_IMAGE"
    assert exc_info.value.field == "image_id"

    bad = copy.deepcopy(sample)
    bad["keypoint_meta"].pop()
    with pytest.raises(PipelineError) as exc_info:
        validate_samples([bad], config)
    assert exc_info.value.code == "E_META_LEN"
    assert exc_info.value.field == "keypoint_meta"

    bad = copy.deepcopy(sample)
    bad["canvas_shape"] = [2, 2]
    with pytest.raises(PipelineError) as exc_info:
        validate_samples([bad], config)
    assert exc_info.value.code == "E_CANVAS_INVALID"
    assert exc_info.value.field == "canvas_shape"

    bad = copy.deepcopy(sample)
    bad["occlusion_ratio"] = 1.2
    with pytest.raises(PipelineError) as exc_info:
        validate_samples([bad], config)
    assert exc_info.value.code == "E_CONFIG_INVALID"
    assert "occlusion_ratio" in exc_info.value.field

    bad = copy.deepcopy(sample)
    bad["candidates"][0]["keypoints"][0]["x"] = 999
    with pytest.raises(PipelineError) as exc_info:
        validate_samples([bad], config)
    assert exc_info.value.code == "E_COORD_RANGE"
    assert "candidates[0].keypoints[0]" in exc_info.value.field

    bad_config = copy.deepcopy(config)
    bad_config["matching"]["threshold"] = 0.0
    with pytest.raises(PipelineError) as exc_info:
        validate_config(bad_config)
    assert exc_info.value.code == "E_THRESHOLD_INVALID"
    assert exc_info.value.field == "matching.threshold"


def test_adaptive_downgrade_and_hard_resource_limit_are_deterministic():
    config = make_config(num_images=5, candidates=20)
    sample = generate_sample(config, 0)
    first = decide_path(sample, 5, config)
    second = decide_path(sample, 5, config)
    assert first == second
    assert first["selected_path"] == "approx"
    assert first["degraded"] is True
    assert "PEAK_RESOLUTION" in first["degradation_categories"]
    assert first["coverage_complete"] is True

    hard_config = copy.deepcopy(config)
    hard_config["adaptive"]["max_candidates_per_image"] = 2
    with pytest.raises(PipelineError) as exc_info:
        decide_path(sample, 5, hard_config)
    assert exc_info.value.code == "E_RESOURCE_LIMIT"
