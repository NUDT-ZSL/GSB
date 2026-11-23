from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from pose_diag.config import PipelineConfig
from pose_diag.consistency import compare_region, compare_stitched
from pose_diag.errors import BusinessError
from pose_diag.grouping import validate_instances
from pose_diag.service import DiagnosticService


def make_service(
    manifest_path: Path,
    state_dir: Path,
    config: PipelineConfig,
) -> DiagnosticService:
    return DiagnosticService(
        manifest_path,
        config,
        state_dir,
        reset_state=True,
    )


def test_batch_is_deterministic_across_services(
    manifest_path: Path, tmp_path: Path, config: PipelineConfig
) -> None:
    first = make_service(manifest_path, tmp_path / "a", config)
    second = make_service(manifest_path, tmp_path / "b", config)
    left = first.run_full("scene-0001")
    right = second.run_full("scene-0001")
    assert first.engines["scene-0001"].instance_canonical_bytes(
        left.instances
    ) == second.engines["scene-0001"].instance_canonical_bytes(right.instances)
    assert len(left.peaks) == len(right.peaks)


def test_center_region_matches_batch_exactly(
    manifest_path: Path, tmp_path: Path, config: PipelineConfig
) -> None:
    service = make_service(manifest_path, tmp_path / "state", config)
    batch = service.run_full("scene-0002")
    row, col = config.center_region
    region = service.run_region("scene-0002", row, col)
    verdict = compare_region(
        batch.instances,
        region.instances,
        batch.peaks,
        region.peaks,
        region.region,
        score_decimals=service.engines["scene-0002"].score_decimals,
        scene_id="scene-0002",
    )
    assert verdict["business_code"] == "OK"
    assert verdict["consistent"] is True


def test_all_regions_stitch_to_batch_exactly(
    manifest_path: Path, tmp_path: Path, config: PipelineConfig
) -> None:
    service = make_service(manifest_path, tmp_path / "state", config)
    batch = service.run_full("scene-0002")
    preview = service.preview_center_then_rest(
        "scene-0002",
        requested_parallel=1,
        latency_budget_ms=10_000,
    )
    comparison = service.compare_scene("scene-0002", preview)
    assert comparison["business_code"] == "OK"
    assert comparison["stitched"]["instance_count"] == len(batch.instances)
    assert all(
        item["business_code"] == "OK" for item in comparison["per_region"]
    )
    assert len(preview["rest"]) + 1 == config.region_rows * config.region_cols


def test_boundary_cutting_grid_still_matches(
    manifest_path: Path, tmp_path: Path, config: PipelineConfig
) -> None:
    boundary_config = replace(
        config,
        region_rows=4,
        region_cols=8,
        center_region=(1, 4),
        latency_budget_ms=10_000,
    )
    service = make_service(manifest_path, tmp_path / "state", boundary_config)
    batch = service.run_full("scene-0002")
    preview = service.preview_center_then_rest(
        "scene-0002",
        requested_parallel=1,
        latency_budget_ms=10_000,
    )
    comparison = service.compare_scene("scene-0002", preview)
    assert comparison["business_code"] == "OK"
    assert comparison["stitched"]["instance_count"] == len(batch.instances)
    # The chosen grid has boundaries at x=80,160,... and cuts through skeletons.
    assert any(
        item["business_code"] == "OK" for item in comparison["per_region"]
    )


def test_peak_mismatch_reports_first_deviation(
    manifest_path: Path, tmp_path: Path, config: PipelineConfig
) -> None:
    service = make_service(manifest_path, tmp_path / "state", config)
    batch = service.run_full("scene-0001")
    center = service.run_region("scene-0001", 2, 2)
    region_with_data = center
    for row in range(config.region_rows):
        for col in range(config.region_cols):
            candidate = service.run_region("scene-0001", row, col)
            if candidate.peaks:
                region_with_data = candidate
                break
        if region_with_data.peaks:
            break
    assert region_with_data.peaks
    altered = copy.deepcopy(region_with_data.peaks)
    altered[0] = replace(
        altered[0],
        refined_x=altered[0].refined_x + 0.01,
    )
    verdict = compare_region(
        batch.instances,
        region_with_data.instances,
        batch.peaks,
        altered,
        region_with_data.region,
        score_decimals=service.engines["scene-0001"].score_decimals,
        scene_id="scene-0001",
    )
    assert verdict["business_code"] == "E_PEAK_MISMATCH"
    assert verdict["first_deviation"]["channel"] == altered[0].channel
    assert verdict["first_deviation"]["full"] is not None
    assert verdict["first_deviation"]["preview"] is not None


def test_block_mismatch_reports_instance_and_type(
    manifest_path: Path, tmp_path: Path, config: PipelineConfig
) -> None:
    service = make_service(manifest_path, tmp_path / "state", config)
    batch = service.run_full("scene-0001")
    preview = service.run_region("scene-0001", 2, 0)
    assert preview.instances
    altered = copy.deepcopy(preview.instances)
    altered[0].score = round(altered[0].score + 0.001, 9)
    verdict = compare_region(
        batch.instances,
        altered,
        batch.peaks,
        preview.peaks,
        preview.region,
        scene_id="scene-0001",
    )
    assert verdict["business_code"] == "E_BLOCK_MISMATCH"
    first = verdict["first_difference"]
    assert first["difference_type"] == "score"
    assert first["full"]["instance_id"] == first["instance_id"]
    assert first["preview"]["score"] != first["full"]["score"]


def test_kp_occupied_and_skeleton_broken(
    manifest_path: Path, tmp_path: Path, config: PipelineConfig
) -> None:
    service = make_service(manifest_path, tmp_path / "state", config)
    batch = service.run_full("scene-0001")
    metadata = service.bundles["scene-0001"].metadata
    first = batch.instances[0]
    duplicate = copy.deepcopy(first)
    duplicate.instance_id = "duplicate-instance"
    with pytest.raises(BusinessError) as caught:
        validate_instances(
            [first, duplicate], metadata, scene_id="scene-0001"
        )
    assert caught.value.code == "E_KP_OCCUPIED"
    assert caught.value.details["keypoint"]["channel"] in {
        peak.channel for peak in first.peaks
    }

    broken = copy.deepcopy(first)
    broken.instance_id = "broken-instance"
    broken.peaks = tuple(
        peak for peak in broken.peaks if peak.channel not in {1, 2}
    )
    with pytest.raises(BusinessError) as caught:
        validate_instances([broken], metadata, scene_id="scene-0001")
    assert caught.value.code == "E_SKELETON_BROKEN"


def test_latency_budget_returns_completed_regions(
    manifest_path: Path, tmp_path: Path, config: PipelineConfig
) -> None:
    service = make_service(manifest_path, tmp_path / "state", config)
    with pytest.raises(BusinessError) as caught:
        service.run_region(
            "scene-0001",
            2,
            2,
            latency_budget_ms=1e-9,
            completed_regions=["r00c00", "r00c01"],
        )
    assert caught.value.code == "E_LATENCY_BUDGET"
    assert caught.value.details["completed_regions"] == ["r00c00", "r00c01"]
    assert caught.value.details["observed_ms"] > 0
    assert caught.value.details["estimate"]["expanded_pixels"] > 0


def test_parallel_request_degrades_to_serial(
    manifest_path: Path, tmp_path: Path, config: PipelineConfig
) -> None:
    limited = replace(config, max_parallel_previews=1)
    service = make_service(manifest_path, tmp_path / "state", limited)
    preview = service.preview_center_then_rest(
        "scene-0001",
        requested_parallel=4,
        latency_budget_ms=10_000,
    )
    assert preview["degradation"]["category"] == "serial_preview"
    assert preview["degradation"]["business_code"] == "DEGRADED_SERIAL_PREVIEW"


def test_stitched_conflict_is_block_mismatch(
    manifest_path: Path, tmp_path: Path, config: PipelineConfig
) -> None:
    service = make_service(manifest_path, tmp_path / "state", config)
    batch = service.run_full("scene-0001")
    left = copy.deepcopy(batch.instances)
    right = copy.deepcopy(batch.instances)
    right[0].score = round(right[0].score + 0.0001, 9)
    verdict = compare_stitched(
        batch.instances, [left, right], scene_id="scene-0001"
    )
    assert verdict["business_code"] == "E_BLOCK_MISMATCH"
    assert verdict["difference_type"] == "conflicting_region_copy"

