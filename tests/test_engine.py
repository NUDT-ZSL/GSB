from __future__ import annotations

import json
from pathlib import Path

import pytest

from ctr_cvr_joint.cache import CacheKey, StageLock
from ctr_cvr_joint.engine import EngineConfig, JointCTREngine
from ctr_cvr_joint.errors import (
    E_CACHE_CORRUPT,
    E_LINEAGE_BROKEN,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    BusinessError,
)
from ctr_cvr_joint.hashing import read_json
from ctr_cvr_joint.samples import apply_label_updates

from .conftest import strict_config, tolerance_config


def _run_full(path, fixtures, config, *, spec_key="spec_v3", stats_key="stats_v1", spec_version=3):
    engine = JointCTREngine(path, config)
    report = engine.run_full(
        fixtures["samples"],
        fixtures[spec_key],
        fixtures[stats_key],
        spec_snapshot_version=spec_version,
    )
    return engine, report


def _core(report):
    return (
        report.data["assembly_summary"]["matrix_sha256"],
        report.data["score_summary"]["score_hash"],
        report.data["accumulation_summary"]["checksum"],
    )


def test_full_run_self_check_and_cache_probe(tmp_path, fixtures):
    engine, report = _run_full(tmp_path / "full", fixtures, strict_config())
    assert report.data["business_code"] == "OK"
    assert report.data["self_check"]["ok"] is True
    assert engine.self_check()["ok"] is True
    probe = engine.explain_request("req-0001")
    assert [node["node"] for node in probe["nodes"]] == [
        "assembly",
        "scoring",
        "accumulation",
    ]
    assembly_components = probe["nodes"][0]["key_components"]
    assert assembly_components["sample_content_hash"]
    assert assembly_components["internal_spec_version"]
    assert assembly_components["spec_checksum"]
    assert assembly_components["statistics_snapshot_version"] == "not_applicable"
    assert probe["nodes"][1]["key_components"]["statistics_snapshot_version"]
    assert probe["nodes"][2]["key_components"]["upstream_product_hash"]
    assert all(node["product_hash"] for node in probe["nodes"])


def test_same_input_reuses_all_nodes(tmp_path, fixtures):
    engine, first = _run_full(tmp_path / "reuse", fixtures, strict_config())
    second = engine.run_full(
        fixtures["samples"],
        fixtures["spec_v3"],
        fixtures["stats_v1"],
        spec_snapshot_version=3,
    )
    assert _core(first) == _core(second)
    for node in ("assembly", "scoring", "accumulation"):
        node_report = second.data["cache"]["nodes"][node]
        assert node_report["recomputed_count"] == 0
        assert node_report["reused_count"] == 12


def test_threshold_change_only_recomputes_scoring(tmp_path, fixtures):
    engine, _ = _run_full(tmp_path / "threshold", fixtures, strict_config())
    old_trace = engine._state["traces"]["req-0001"]
    engine.config = tolerance_config()
    report = engine.run_incremental(
        fixtures["samples"],
        fixtures["spec_v3"],
        fixtures["stats_v1"],
        spec_snapshot_version=3,
        change_kind="threshold",
    )
    nodes = report.data["cache"]["nodes"]
    assert nodes["assembly"]["recomputed_count"] == 0
    assert nodes["scoring"]["recomputed_count"] == 12
    assert nodes["accumulation"]["recomputed_count"] == 0
    assert report.data["invalidation"]["affected_nodes"] == ["scoring"]
    with pytest.raises(BusinessError) as excinfo:
        engine.replay_trace(old_trace)
    assert excinfo.value.code == E_LINEAGE_BROKEN
    assert excinfo.value.details["request_id"] == "req-0001"
    assert any(
        item["dependency"] == "scoring_config"
        for item in excinfo.value.details["invalid_versions"]
    )
    fresh = JointCTREngine(tmp_path / "threshold_full", tolerance_config())
    full = fresh.run_full(
        fixtures["samples"],
        fixtures["spec_v3"],
        fixtures["stats_v1"],
        spec_snapshot_version=3,
    )
    assert _core(report) == _core(full)


def test_statistics_update_is_targeted_and_matches_full(tmp_path, fixtures):
    engine, _ = _run_full(tmp_path / "stats", fixtures, tolerance_config())
    report = engine.run_incremental(
        fixtures["samples"],
        fixtures["spec_v3"],
        fixtures["stats_v2"],
        change_kind="stats",
    )
    affected = set(
        report.data["cache"]["nodes"]["scoring"]["affected_samples"]
    )
    assert affected == {"req-0003", "req-0007"}
    assert report.data["cache"]["nodes"]["assembly"]["recomputed_count"] == 0
    assert report.data["cache"]["nodes"]["scoring"]["recomputed_count"] == 2
    assert report.data["cache"]["nodes"]["accumulation"]["recomputed_count"] == 2
    fresh, full = _run_full(
        tmp_path / "stats_full",
        fixtures,
        tolerance_config(),
        stats_key="stats_v2",
    )
    assert _core(report) == _core(full)


def test_label_correction_reuses_assembly_and_scores(tmp_path, fixtures):
    engine, _ = _run_full(tmp_path / "labels", fixtures, tolerance_config(), stats_key="stats_v2")
    updates = {
        "req-0002": {"label_click": 0, "label_convert": 0},
        "req-0005": {"label_click": 0, "label_convert": 0},
    }
    samples = apply_label_updates(fixtures["samples"], updates)
    report = engine.run_incremental(
        samples,
        fixtures["spec_v3"],
        fixtures["stats_v2"],
        change_kind="labels",
    )
    nodes = report.data["cache"]["nodes"]
    assert nodes["assembly"]["recomputed_count"] == 0
    assert nodes["scoring"]["recomputed_count"] == 0
    assert nodes["accumulation"]["recomputed_count"] == 2
    assert set(report.data["invalidation"]["label_corrected_request_ids"]) == set(
        updates
    )
    fresh = JointCTREngine(tmp_path / "labels_full", tolerance_config())
    full = fresh.run_full(
        samples,
        fixtures["spec_v3"],
        fixtures["stats_v2"],
        spec_snapshot_version=3,
    )
    assert _core(report) == _core(full)


def test_feature_spec_revision_invalidates_all_downstream(tmp_path, fixtures):
    engine, _ = _run_full(tmp_path / "spec", fixtures, tolerance_config(), stats_key="stats_v2")
    old_trace = engine._state["traces"]["req-0001"]
    report = engine.run_incremental(
        fixtures["samples"],
        fixtures["spec_v3_rev2"],
        fixtures["stats_v2"],
        spec_snapshot_version=4,
        change_kind="spec",
    )
    for node in ("assembly", "scoring", "accumulation"):
        assert report.data["cache"]["nodes"][node]["recomputed_count"] == 12
    assert "numeric[0].mean" in report.data["invalidation"]["changed_spec_paths"]
    with pytest.raises(BusinessError) as excinfo:
        engine.replay_trace(old_trace)
    assert excinfo.value.code == E_LINEAGE_BROKEN
    assert any(
        item["dependency"] == "feature_spec"
        for item in excinfo.value.details["invalid_versions"]
    )
    fresh = JointCTREngine(tmp_path / "spec_full", tolerance_config())
    full = fresh.run_full(
        fixtures["samples"],
        fixtures["spec_v3_rev2"],
        fixtures["stats_v2"],
        spec_snapshot_version=4,
    )
    assert _core(report) == _core(full)


def test_trace_replay_rejects_tampered_trace(tmp_path, fixtures):
    engine, report = _run_full(tmp_path / "tamper", fixtures, strict_config())
    trace = json.loads(json.dumps(report.data["traces"][0]))
    trace["score"]["click"]["probability"] = 0.123
    with pytest.raises(BusinessError) as excinfo:
        engine.replay_trace(trace, require_current=False)
    assert excinfo.value.code == E_CACHE_CORRUPT


def test_tampered_cache_fails_self_check(tmp_path, fixtures):
    engine, report = _run_full(tmp_path / "cache", fixtures, strict_config())
    key = report.data["lineage_index"]["req-0001"]["score_cache_key"]
    path = engine.cache._path("scoring", key)
    payload = read_json(path)
    payload["product"]["row"]["click"]["probability"] = 0.123
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BusinessError) as excinfo:
        engine.self_check()
    assert excinfo.value.code == E_CACHE_CORRUPT


def test_snapshot_rollback_and_corruption(tmp_path, fixtures):
    engine, _ = _run_full(
        tmp_path / "snapshot",
        fixtures,
        strict_config(),
        spec_key="spec_v3_rev2",
        spec_version=4,
    )
    with pytest.raises(BusinessError) as excinfo:
        engine.run_full(
            fixtures["samples"],
            fixtures["spec_v3"],
            fixtures["stats_v1"],
            spec_snapshot_version=3,
        )
    assert excinfo.value.code == E_SNAPSHOT_ROLLBACK

    corrupt_engine, _ = _run_full(tmp_path / "snapshot_corrupt", fixtures, strict_config())
    info = corrupt_engine.snapshots.current_info("feature_spec")
    assert info is not None
    path = Path(info.path)
    wrapper = read_json(path)
    wrapper["payload"]["spec"]["source_version"] = 999
    path.write_text(json.dumps(wrapper), encoding="utf-8")
    with pytest.raises(BusinessError) as excinfo:
        corrupt_engine.snapshots.load("feature_spec", info.version)
    assert excinfo.value.code == E_CACHE_CORRUPT


def test_stage_lock_is_non_blocking(tmp_path, fixtures):
    engine, _ = _run_full(tmp_path / "lock", fixtures, strict_config())
    with StageLock(engine.workspace):
        with pytest.raises(BusinessError) as excinfo:
            engine.run_incremental(
                fixtures["samples"],
                fixtures["spec_v3"],
                fixtures["stats_v1"],
                spec_snapshot_version=3,
            )
    assert excinfo.value.code == E_STAGE_BUSY


def test_resource_limit_and_parallel_degradation(tmp_path, fixtures):
    limited = JointCTREngine(
        tmp_path / "limited",
        EngineConfig(
            seed=20261124,
            threshold=strict_config().threshold,
            max_feature_matrix_bytes=8,
        ),
    )
    with pytest.raises(BusinessError) as excinfo:
        limited.run_full(
            fixtures["samples"],
            fixtures["spec_v3"],
            fixtures["stats_v1"],
            spec_snapshot_version=3,
        )
    assert excinfo.value.code == E_RESOURCE_LIMIT

    degraded = JointCTREngine(
        tmp_path / "degraded",
        EngineConfig(
            seed=20261124,
            threshold=strict_config().threshold,
            max_parallel_nodes=2,
            parallel_nodes=8,
        ),
    )
    report = degraded.run_full(
        fixtures["samples"],
        fixtures["spec_v3"],
        fixtures["stats_v1"],
        spec_snapshot_version=3,
    )
    assert report.data["degraded"] == ["parallelism_capped"]
    assert report.data["self_check"]["ok"] is True


def test_cache_key_changes_with_dependency(tmp_path, fixtures):
    engine, report = _run_full(tmp_path / "keys", fixtures, tolerance_config())
    first_key = report.data["lineage_index"]["req-0001"]["score_cache_key"]
    next_report = engine.run_incremental(
        fixtures["samples"],
        fixtures["spec_v3"],
        fixtures["stats_v2"],
        change_kind="stats",
    )
    # req-0001 does not use the changed city series and therefore keeps its key.
    assert (
        next_report.data["lineage_index"]["req-0001"]["score_cache_key"]
        == first_key
    )
    assert (
        next_report.data["lineage_index"]["req-0003"]["score_cache_key"]
        != report.data["lineage_index"]["req-0003"]["score_cache_key"]
    )

