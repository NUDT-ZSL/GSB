"""Dependency-failure propagation and revision suffix recomputation."""
from __future__ import annotations

import dataclasses

from scene_pipeline.incremental import (
    STAGE_ORDER,
    manifest_fingerprint,
    plan_incremental,
)
from scene_pipeline.observations import generate_scene_bundle
from scene_pipeline.scheduler import run_schedule
from scene_pipeline.snapshot import SnapshotStore
from scene_pipeline.synth import POINT_COUNT
from tests.conftest import TEST_POINT_COUNT


def _run(manifests, config, paths):
    for manifest in manifests:
        generate_scene_bundle(manifest, paths["observations"], point_count=TEST_POINT_COUNT)
    return run_schedule(
        manifests=manifests,
        config=config,
        observation_dir=paths["observations"],
        snapshot_root=paths["snapshots"],
        point_count=TEST_POINT_COUNT,
        vertex_estimate=128,
    )


def test_new_scene_recomputes_everything(small_config, manifest, paths):
    store = SnapshotStore(paths["snapshots"])
    plan = plan_incremental(None, manifest, store)
    assert plan.recomputed_stages == list(STAGE_ORDER)
    assert plan.reused_stages == []


def test_geometry_mode_switch_invalidates_geometry_texture(
        small_config, manifest, paths):
    store = SnapshotStore(paths["snapshots"])
    _run([manifest], small_config, paths)
    fingerprints = manifest_fingerprint(manifest)

    revised = dataclasses.replace(
        manifest, scene_revision=2, geometry_mode="shell"
    )
    plan = plan_incremental(
        manifest, revised, store, previous_fingerprints=fingerprints
    )
    assert plan.invalidated_stages == ["geometry", "texture"]
    assert plan.reused_stages == ["ingest", "registration"]
    assert plan.recomputed_stages == ["geometry", "texture"]


def test_camera_pose_change_invalidates_registration_suffix(
        small_config, manifest, paths):
    store = SnapshotStore(paths["snapshots"])
    _run([manifest], small_config, paths)
    fingerprints = manifest_fingerprint(manifest)

    cameras = [dataclasses.replace(
        manifest.camera_poses[0],
        fx=manifest.camera_poses[0].fx + 1.0,
    )] + list(manifest.camera_poses[1:])
    revised = dataclasses.replace(
        manifest, scene_revision=2, camera_poses=cameras
    )
    plan = plan_incremental(
        manifest, revised, store, previous_fingerprints=fingerprints
    )
    assert "registration" in plan.invalidated_stages
    assert plan.reused_stages == ["ingest"]


def test_texture_seed_change_invalidates_only_texture(
        small_config, manifest, paths):
    store = SnapshotStore(paths["snapshots"])
    _run([manifest], small_config, paths)
    fingerprints = manifest_fingerprint(manifest)

    revised = dataclasses.replace(
        manifest, scene_revision=2, texture_seed=manifest.texture_seed + 99
    )
    plan = plan_incremental(
        manifest, revised, store, previous_fingerprints=fingerprints
    )
    assert plan.invalidated_stages == ["texture"]
    assert plan.reused_stages == ["ingest", "registration", "geometry"]


def test_regression_in_revision_is_rejected(small_config, manifest, paths):
    store = SnapshotStore(paths["snapshots"])
    older = dataclasses.replace(manifest, scene_revision=0)
    import pytest

    with pytest.raises(ValueError):
        plan_incremental(manifest, older, store,
                         previous_fingerprints=manifest_fingerprint(manifest))


def test_incremental_run_matches_full_rerun_bytes(small_config, manifest, paths):
    from scene_pipeline.hashing import checksum_file

    _run([manifest], small_config, paths)
    store = SnapshotStore(paths["snapshots"])
    fingerprints = manifest_fingerprint(manifest)
    revised = dataclasses.replace(
        manifest, scene_revision=2, geometry_mode="shell"
    )
    plan = plan_incremental(
        manifest, revised, store, previous_fingerprints=fingerprints
    )

    # Snapshot the reusable upstream artifacts before the incremental run.
    reusable_files = {}
    for stage in plan.reused_stages:
        version = store.current_version(manifest.scene_id, stage)
        version_dir = store.version_dir(manifest.scene_id, stage, version)
        reusable_files[stage] = checksum_file(f"{version_dir}/snapshot.json")

    generate_scene_bundle(revised, paths["observations"], point_count=TEST_POINT_COUNT)
    run_schedule(
        manifests=[revised],
        config=small_config,
        observation_dir=paths["observations"],
        snapshot_root=paths["snapshots"],
        point_count=TEST_POINT_COUNT,
        vertex_estimate=128,
        incremental_plans={revised.scene_id: plan},
    )
    for stage, expected in reusable_files.items():
        version = store.current_version(manifest.scene_id, stage)
        version_dir = store.version_dir(manifest.scene_id, stage, version)
        assert checksum_file(f"{version_dir}/snapshot.json") == expected
