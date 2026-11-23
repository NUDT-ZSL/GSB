from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from pose_diag.config import PipelineConfig
from pose_diag.errors import BusinessError
from pose_diag.grouping import validate_metadata
from pose_diag.heatmap import (
    detect_and_refine,
    detect_peaks,
    peaks_bytes,
    refine_peaks,
    validate_heatmap_channels,
)
from pose_diag.io_utils import atomic_write_text
from pose_diag.samples import (
    generate_samples,
    load_manifest,
    load_scene_sample,
    reconstruct_heatmaps_for_scene,
    validate_scene_record,
)
from pose_diag.scoring import validate_scoring


def test_generation_is_deterministic(tmp_path: Path, config: PipelineConfig) -> None:
    first = generate_samples(tmp_path / "first", config, scene_count=1)
    second = generate_samples(tmp_path / "second", config, scene_count=1)
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    first_scene = first.scene_paths[0]
    second_scene = second.scene_paths[0]
    assert (first_scene / "scene.json").read_bytes() == (
        second_scene / "scene.json"
    ).read_bytes()
    for name in ("canvas.npy", "truth_heatmaps.npy", "observation_heatmaps.npy"):
        left = np.load(first_scene / name, allow_pickle=False)
        right = np.load(second_scene / name, allow_pickle=False)
        assert np.array_equal(left, right)


def test_fixed_seed_reconstructs_truth_and_observation(
    manifest_path: Path, config: PipelineConfig
) -> None:
    record = load_manifest(manifest_path)[0]
    sample = load_scene_sample(manifest_path, record, config)
    truth, observation = reconstruct_heatmaps_for_scene(
        record, sample.metadata["persons"]
    )
    assert np.array_equal(truth, sample.truth_heatmaps)
    assert np.array_equal(observation, sample.observation_heatmaps)


def test_peak_detection_is_byte_deterministic(config: PipelineConfig) -> None:
    heatmaps = np.zeros((2, 12, 12), dtype=np.float32)
    heatmaps[0, 3, 3] = 0.9
    heatmaps[0, 8, 4] = 0.9
    heatmaps[1, 3, 3] = 0.9
    first = detect_and_refine(
        heatmaps,
        2,
        config,
        scene_id="tie-scene",
    )
    second = detect_and_refine(
        heatmaps,
        2,
        config,
        scene_id="tie-scene",
    )
    assert peaks_bytes(first) == peaks_bytes(second)
    assert [(peak.channel, peak.y, peak.x) for peak in first] == [
        (0, 3, 3),
        (0, 8, 4),
        (1, 3, 3),
    ]


def test_channel_mismatch_business_error() -> None:
    with pytest.raises(BusinessError) as caught:
        validate_heatmap_channels(
            np.zeros((3, 10, 10), dtype=np.float32), 4, "bad-channel"
        )
    assert caught.value.code == "E_CHANNEL_MISMATCH"
    assert caught.value.field == "num_keypoints"
    assert caught.value.scene_id == "bad-channel"


def test_quantization_invalid(config: PipelineConfig) -> None:
    invalid = replace(config, quantization_decimals=7)
    with pytest.raises(BusinessError) as caught:
        refine_peaks([], np.zeros((1, 4, 4), dtype=np.float32), invalid)
    assert caught.value.code == "E_QUANT_INVALID"


def test_refine_window_clamps_and_marks(config: PipelineConfig) -> None:
    heatmaps = np.zeros((1, 8, 8), dtype=np.float32)
    heatmaps[0, 0, 0] = 1.0
    heatmaps[0, 0, 1] = 0.4
    heatmaps[0, 1, 0] = 0.4
    raw = detect_peaks(
        heatmaps,
        expected_channels=1,
        threshold=0.02,
        neighborhood_radius=1,
    )
    refined = refine_peaks(raw, heatmaps, config)
    assert refined
    assert refined[0].window_clipped is True
    assert refined[0].refined_x >= 0
    assert refined[0].refined_y >= 0


def test_input_validation_codes(
    manifest_path: Path, config: PipelineConfig
) -> None:
    record = load_manifest(manifest_path)[0]
    sample = load_scene_sample(manifest_path, record, config)

    with pytest.raises(BusinessError) as caught:
        validate_scene_record(replace(record, canvas_shape=(0, 640, 3)), config)
    assert caught.value.code == "E_CANVAS_INVALID"

    with pytest.raises(BusinessError) as caught:
        validate_scene_record(replace(record, occlusion_ratio=1.2), config)
    assert caught.value.code == "E_OCCLUSION_RATIO"

    bad_metadata = dict(sample.metadata)
    bad_metadata["persons"] = list(bad_metadata["persons"])[:-1]
    with pytest.raises(BusinessError) as caught:
        validate_metadata(
            bad_metadata,
            expected_channels=record.num_keypoints,
            expected_persons=record.person_count,
            scene_id=record.scene_id,
        )
    assert caught.value.code == "E_PERSON_COUNT_MISMATCH"
    assert caught.value.field == "person_count"

    with pytest.raises(BusinessError) as caught:
        validate_scoring(
            {**sample.scoring, "match_threshold": 0.0},
            scene_id=record.scene_id,
        )
    assert caught.value.code == "E_MATCH_THRESHOLD"


def test_duplicate_manifest_scene_id(tmp_path: Path) -> None:
    line = (
        '{"canvas_shape":[480,640,3],"heatmap_seed":1,"num_keypoints":12,'
        '"occlusion_ratio":0.1,"person_count":2,"policy":"p",'
        '"sample_dir":"scenes/x","scene_id":"dup","scene_seed":7}'
    )
    path = tmp_path / "manifest.jsonl"
    atomic_write_text(path, f"{line}\n{line}\n")
    with pytest.raises(BusinessError) as caught:
        load_manifest(path)
    assert caught.value.code == "E_DUPLICATE_SCENE_ID"
    assert caught.value.scene_id == "dup"


def test_resource_limits(manifest_path: Path, config: PipelineConfig) -> None:
    record = load_manifest(manifest_path)[0]
    with pytest.raises(BusinessError) as caught:
        validate_scene_record(record, replace(config, max_scene_pixels=1))
    assert caught.value.code == "E_RESOURCE_LIMIT"
    assert caught.value.field == "canvas_shape"

    with pytest.raises(BusinessError) as caught:
        validate_scene_record(
            record, replace(config, max_peak_memory_mb=0.000001)
        )
    assert caught.value.code == "E_RESOURCE_LIMIT"
    assert caught.value.field == "max_peak_memory_mb"

