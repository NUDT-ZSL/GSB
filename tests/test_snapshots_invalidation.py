from __future__ import annotations

import json
from pathlib import Path

import pytest

from pose_diag.config import PipelineConfig
from pose_diag.errors import BusinessError
from pose_diag.io_utils import canonical_json_text
from pose_diag.samples import load_manifest, load_scene_sample
from pose_diag.service import DiagnosticService
from pose_diag.snapshots import SnapshotStore


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


def test_snapshot_commit_rollback_and_corruption(
    manifest_path: Path, tmp_path: Path, config: PipelineConfig
) -> None:
    record = load_manifest(manifest_path)[0]
    sample = load_scene_sample(manifest_path, record, config)
    store = SnapshotStore(tmp_path / "state", record.scene_id)
    bundle = store.initialize(
        sample.observation_heatmaps,
        sample.metadata,
        sample.scoring,
        expected_channels=record.num_keypoints,
        person_count=record.person_count,
    )
    assert bundle.versions["scoring"] == 1
    new_scoring = dict(sample.scoring)
    new_scoring["match_threshold"] = 0.4
    committed = store.commit("scoring", 2, new_scoring)
    assert committed.version == 2
    assert store.load("scoring").version == 2
    assert store.load_version("scoring", 1).version == 1

    with pytest.raises(BusinessError) as caught:
        store.commit("scoring", 1, sample.scoring)
    assert caught.value.code == "E_SNAPSHOT_ROLLBACK"
    assert caught.value.details["current_version"] == 2

    path = store.path_for("scoring")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["payload"]["tampered"] = True
    path.write_text(canonical_json_text(document), encoding="utf-8")
    with pytest.raises(BusinessError) as caught:
        store.load("scoring")
    assert caught.value.code == "E_SNAPSHOT_CORRUPT"
    assert caught.value.details["category"] == "scoring"
    assert caught.value.details["version"] == 2


def test_targeted_invalidation_and_stale_rejection(
    manifest_path: Path, tmp_path: Path, config: PipelineConfig
) -> None:
    service = make_service(manifest_path, tmp_path / "state", config)
    initial = service.run_full("scene-0002")
    old_instance = initial.instances[0]

    scoring_plan = service.make_scoring_update("scene-0002", 2)
    assert scoring_plan["change_category"] == "scoring"
    assert set(scoring_plan["reusable_stages"]) >= {"peaks", "refine", "group"}
    scoring_batch = service.run_full("scene-0002")
    assert scoring_batch.cache_stats["peaks"]["hits"] >= 1
    assert scoring_batch.cache_stats["group"]["hits"] >= 1
    assert scoring_batch.cache_stats["score"]["misses"] >= 1
    with pytest.raises(BusinessError) as caught:
        service.reject_if_stale("scene-0002", old_instance)
    assert caught.value.code == "E_STALE_RESULT"
    assert caught.value.details["version_mismatches"]["scoring"][
        "recorded_version"
    ] == 1

    fresh = scoring_batch.instances[0]
    replay = service.replay_instance("scene-0002", fresh.instance_id)
    assert replay["business_code"] == "OK"
    assert replay["score_equal"] is True
    assert replay["instance_set_equal"] is True

    metadata_plan = service.make_metadata_correction("scene-0002", 2)
    assert metadata_plan["change_category"] == "keypoint_metadata"
    assert 0 < len(metadata_plan["affected_regions"]) < 25
    metadata_batch = service.run_full("scene-0002")
    assert metadata_batch.cache_stats["peaks"]["hits"] >= 1
    assert metadata_batch.cache_stats["group"]["misses"] >= 1
    assert metadata_batch.cache_stats["group"]["hits"] == 0

    heatmap_plan = service.make_heatmap_update("scene-0002", 2)
    assert heatmap_plan["change_category"] == "heatmap_observation"
    assert len(heatmap_plan["affected_regions"]) == 25
    heatmap_batch = service.run_full("scene-0002")
    assert heatmap_batch.cache_stats["peaks"]["hits"] == 0
    assert heatmap_batch.cache_stats["group"]["misses"] >= 1
    assert heatmap_batch.cache_stats["score"]["misses"] >= 1
    old_replay = service.replay_instance("scene-0002", old_instance.instance_id)
    assert old_replay["business_code"] == "OK"
    assert old_replay["score_equal"] is True
    assert old_replay["instance_set_equal"] is True


def test_checksum_mismatch_is_stale(
    manifest_path: Path, tmp_path: Path, config: PipelineConfig
) -> None:
    service = make_service(manifest_path, tmp_path / "state", config)
    batch = service.run_full("scene-0001")
    instance = batch.instances[0]
    instance.snapshot_checksums["scoring"] = "0" * 64
    with pytest.raises(BusinessError) as caught:
        service.reject_if_stale("scene-0001", instance)
    assert caught.value.code == "E_STALE_RESULT"
    assert "scoring" in caught.value.details["checksum_mismatches"]


def test_snapshot_payload_is_replaced_whole(
    manifest_path: Path, tmp_path: Path, config: PipelineConfig
) -> None:
    service = make_service(manifest_path, tmp_path / "state", config)
    service.ensure_scene("scene-0001")
    before = service.bundles["scene-0001"].metadata
    updated = dict(before)
    updated["replacement_marker"] = "whole-document-v2"
    service.commit_snapshot("scene-0001", "keypoint_metadata", 2, updated)
    after = service.bundles["scene-0001"].metadata
    assert after["replacement_marker"] == "whole-document-v2"
    assert "metadata_revision" not in after

