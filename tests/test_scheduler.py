"""Deterministic scheduling, worker isolation and structured failure codes."""
from __future__ import annotations

import json

from scene_pipeline.fault_injection import FaultPlan
from scene_pipeline.manifest import priority_order, write_manifest
from scene_pipeline.observations import generate_scene_bundle
from scene_pipeline.scheduler import deterministic_batches, run_schedule
from scene_pipeline.synth import POINT_COUNT
from tests.conftest import TEST_POINT_COUNT


def _prepare(manifests, observation_dir):
    for manifest in manifests:
        generate_scene_bundle(manifest, observation_dir, point_count=TEST_POINT_COUNT)


def test_priority_order_is_deterministic(manifest):
    from tests.conftest import _make_manifest

    manifests = [_make_manifest(i) for i in range(5)]
    ordered = priority_order(manifests)
    keys = [(-item.priority, item.scene_id) for item in ordered]
    assert keys == sorted(keys)


def test_deterministic_batch_boundaries(manifest):
    from tests.conftest import _make_manifest

    manifests = [_make_manifest(i) for i in range(7)]
    batches = deterministic_batches(manifests, scenes_per_batch=3)
    assert [len(batch) for batch in batches] == [3, 3, 1]
    flat = [scene.scene_id for batch in batches for scene in batch]
    assert flat == [scene.scene_id for scene in priority_order(manifests)]


def test_worker_failure_does_not_stop_other_scenes(small_config, paths, tmp_path):
    from tests.conftest import _make_manifest

    manifests = [_make_manifest(i) for i in range(4)]
    _prepare(manifests, paths["observations"])
    faults = {
        "scene_001": {
            "registration": {
                "code": "E_WORKER_FAULT",
                "message": "boom",
            }
        }
    }
    fault_path = tmp_path / "faults.json"
    fault_path.write_text(json.dumps(faults), encoding="utf-8")

    report = run_schedule(
        manifests=manifests,
        config=small_config,
        observation_dir=paths["observations"],
        snapshot_root=paths["snapshots"],
        point_count=TEST_POINT_COUNT,
        vertex_estimate=128,
        fault_path=str(fault_path),
    )
    failed = {item["scene_id"]: item for item in report.failures}
    assert "scene_001" in failed
    assert failed["scene_001"]["code"] == "E_WORKER_FAULT"
    assert failed["scene_001"]["stage"] == "registration"
    successful = {
        result["scene_id"]
        for batch in report.batches
        for result in batch.results
        if result["success"]
    }
    assert {"scene_000", "scene_002", "scene_003"}.issubset(successful)


def test_fault_plan_codes_map_to_business_errors(tmp_path):
    from scene_pipeline.errors import (
        E_FACE_BUDGET,
        E_TEX_CONSISTENCY,
        FaceBudgetError,
        TextureConsistencyError,
    )

    path = tmp_path / "faults.json"
    path.write_text(json.dumps({
        "a": {"geometry": {"code": E_FACE_BUDGET}},
        "b": {"texture": {"code": E_TEX_CONSISTENCY}},
    }), encoding="utf-8")
    plan = FaultPlan.load(str(path))
    try:
        plan.trigger("a", "geometry")
    except FaceBudgetError as exc:
        assert exc.code == E_FACE_BUDGET
    try:
        plan.trigger("b", "texture")
    except TextureConsistencyError as exc:
        assert exc.code == E_TEX_CONSISTENCY


def test_schedule_report_is_json_serializable(small_config, paths, tmp_path):
    from tests.conftest import _make_manifest

    manifests = [_make_manifest(i) for i in range(3)]
    _prepare(manifests, paths["observations"])
    report = run_schedule(
        manifests=manifests,
        config=small_config,
        observation_dir=paths["observations"],
        snapshot_root=paths["snapshots"],
        point_count=TEST_POINT_COUNT,
        vertex_estimate=128,
    )
    encoded = json.dumps(report.to_dict(), sort_keys=True)
    assert json.loads(encoded)["ordered_scene_ids"] == report.ordered_scene_ids
