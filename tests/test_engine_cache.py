from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from rope_attention.attention import NumericTolerance
from rope_attention.cache import CacheStore
from rope_attention.component import RopeAttentionComponent
from rope_attention.config import parse_config
from rope_attention.data import sample_configs
from rope_attention.engine import (
    AttentionEngine,
    assert_results_byte_identical,
    retarget_tasks,
)
from rope_attention.errors import E_CACHE_CORRUPT, E_RESOURCE_LIMIT, RopeError
from rope_attention.migration import migrate
from rope_attention.models import ResourceLimits
from rope_attention.snapshot import ConfigSnapshot


def _v3_text(config, *, block_size=6, pos_scale=1.0) -> str:
    document = {
        "base_theta": config.base_theta,
        "block_size": block_size,
        "frequency_adjustment": config.effective_frequency_adjustment.to_dict(),
        "per_head_scale": list(config.effective_per_head_scale),
        "pos_scale": pos_scale,
        "variant": config.effective_variant,
    }
    return "v3\n" + json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"


def _full_recompute(tasks, tensors, snapshot):
    engine = AttentionEngine(None, use_cache=False, validate_reference=True)
    return engine.compute_all(tasks, tensors, snapshot)


def test_exact_cache_reuse_and_key_audit(sample_root: Path, sample_inputs) -> None:
    tasks, tensors = sample_inputs
    component = RopeAttentionComponent(sample_root / "state")
    snapshot, _ = component.commit_config(
        sample_configs()["v1"],
        source_name="v1",
        version=1,
    )
    tasks_v1 = retarget_tasks(tasks, snapshot.config)
    first = component.compute(tasks_v1, tensors, snapshot=snapshot)
    second = component.compute(tasks_v1, tensors, snapshot=snapshot)
    assert first.recomputed_nodes
    assert second.recomputed_nodes == ()
    assert all(
        first.cases[case_id].byte_view() == second.cases[case_id].byte_view()
        for case_id in first.cases
    )
    required = {
        "base_theta",
        "block_content_hash",
        "block_size",
        "causal",
        "config_version",
        "pos_scale",
        "upstream_node_hash",
    }
    for case in first.cases.values():
        for event in case.node_events:
            assert required <= set(event.key_parts)
    assert component.verify_cache()["ok"]


def test_raw_cache_requires_explicit_alias_for_different_key(tmp_path: Path) -> None:
    store = CacheStore(tmp_path / "cache")
    calls = {"count": 0}

    def compute():
        calls["count"] += 1
        return {"x": np.asarray([1.0, 2.0])}, {"source": "test"}

    first = store.get_or_compute(
        node="probe",
        case_id="case",
        key_parts={"key_variant": "a", "config_version": "v1"},
        semantic_fingerprint="same-semantics",
        compute=compute,
    )
    second = store.get_or_compute(
        node="probe",
        case_id="case",
        key_parts={"key_variant": "b", "config_version": "v1"},
        semantic_fingerprint="same-semantics",
        compute=compute,
    )
    assert first.key != second.key
    assert first.status == "computed"
    assert second.status == "computed"
    assert calls["count"] == 2

    third = store.get_or_compute(
        node="probe",
        case_id="case",
        key_parts={"key_variant": "c", "config_version": "v1"},
        semantic_fingerprint="same-semantics",
        compute=compute,
        allow_semantic_alias=True,
    )
    assert third.status == "hit_semantic"
    assert calls["count"] == 2


def test_cross_version_alias_reuses_all_nodes(sample_root: Path, sample_inputs) -> None:
    tasks, tensors = sample_inputs
    component = RopeAttentionComponent(sample_root / "state")
    v1_snapshot, _ = component.commit_config(
        sample_configs()["v1"],
        source_name="v1",
        version=1,
    )
    tasks_v1 = retarget_tasks(tasks, v1_snapshot.config)
    baseline = component.compute(tasks_v1, tensors, snapshot=v1_snapshot)
    upgrade = migrate(v1_snapshot.config, "v3")
    assert upgrade.target_text is not None
    v3_snapshot, _ = component.commit_config(
        upgrade.target_text,
        source_name="v1-to-v3",
        version=2,
    )
    tasks_v3 = retarget_tasks(tasks, v3_snapshot.config)
    upgraded = component.compute(tasks_v3, tensors, snapshot=v3_snapshot)
    assert upgraded.recomputed_nodes == ()
    assert len(upgraded.reused_nodes) == len(baseline.cases) * 14
    assert all(
        baseline.cases[case_id].byte_view()
        == upgraded.cases[case_id].byte_view()
        for case_id in baseline.cases
    )
    assert all(
        event.status == "hit_semantic"
        for case in upgraded.cases.values()
        for event in case.node_events
    )


def test_block_size_change_reuses_only_upstream_and_matches_full(
    sample_root: Path, sample_inputs
) -> None:
    tasks, tensors = sample_inputs
    component = RopeAttentionComponent(sample_root / "state")
    v1_snapshot, _ = component.commit_config(
        sample_configs()["v1"],
        source_name="v1",
        version=1,
    )
    component.compute(
        retarget_tasks(tasks, v1_snapshot.config),
        tensors,
        snapshot=v1_snapshot,
    )
    upgrade = migrate(v1_snapshot.config, "v3")
    assert upgrade.target_text is not None
    v3_snapshot, _ = component.commit_config(
        upgrade.target_text,
        source_name="v3",
        version=2,
    )
    component.compute(
        retarget_tasks(tasks, v3_snapshot.config),
        tensors,
        snapshot=v3_snapshot,
    )
    block_snapshot, _ = component.commit_config(
        _v3_text(v3_snapshot.config, block_size=9),
        source_name="block-nine",
        version=3,
    )
    tasks_block = retarget_tasks(tasks, block_snapshot.config)
    plan = component.invalidation_plan(
        tasks_block,
        v3_snapshot,
        block_snapshot,
    )
    incremental = component.compute(tasks_block, tensors, snapshot=block_snapshot)
    full = _full_recompute(tasks_block, tensors, block_snapshot)
    byte_check = assert_results_byte_identical(incremental, full)
    assert byte_check["all_byte_identical"]
    assert plan["predicted_reuse_nodes"] == ["position_angles", "qk_rotation"]
    for case in incremental.cases.values():
        statuses = {event.node: event.status for event in case.node_events}
        assert statuses["position_angles"] == "hit_semantic"
        assert statuses["qk_rotation"] == "hit_semantic"
        assert any(
            node.startswith("block_scores[") and status == "computed"
            for node, status in statuses.items()
        )


def test_position_scale_change_recomputes_entire_suffix(
    sample_root: Path, sample_inputs
) -> None:
    tasks, tensors = sample_inputs
    component = RopeAttentionComponent(sample_root / "state")
    v3_snapshot, _ = component.commit_config(
        sample_configs()["v3"],
        source_name="v3",
        version=1,
    )
    component.compute(
        retarget_tasks(tasks, v3_snapshot.config),
        tensors,
        snapshot=v3_snapshot,
    )
    changed_snapshot, _ = component.commit_config(
        _v3_text(v3_snapshot.config, pos_scale=1.1),
        source_name="positions-scaled",
        version=2,
    )
    changed_tasks = retarget_tasks(tasks, changed_snapshot.config)
    incremental = component.compute(
        changed_tasks,
        tensors,
        snapshot=changed_snapshot,
    )
    full = _full_recompute(changed_tasks, tensors, changed_snapshot)
    assert assert_results_byte_identical(incremental, full)["all_byte_identical"]
    for case in incremental.cases.values():
        statuses = {event.node: event.status for event in case.node_events}
        assert statuses["position_angles"] == "computed"
        assert statuses["qk_rotation"] == "computed"


def test_corrupt_cache_entry_is_reported(sample_root: Path, sample_inputs) -> None:
    tasks, tensors = sample_inputs
    component = RopeAttentionComponent(sample_root / "state")
    snapshot, _ = component.commit_config(
        sample_configs()["v1"],
        source_name="v1",
        version=1,
    )
    subset = [tasks[0]]
    subset_tensors = {tasks[0].case_id: tensors[tasks[0].case_id]}
    result = component.compute(subset, subset_tensors, snapshot=snapshot)
    position = next(
        event
        for event in result.cases[tasks[0].case_id].node_events
        if event.node == "position_angles"
    )
    path = component.cache.entry_path(position.key)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["payload"]["metadata"]["pairing"] = "corrupt"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(RopeError) as captured:
        component.compute(subset, subset_tensors, snapshot=snapshot)
    assert captured.value.code == E_CACHE_CORRUPT


def test_resource_limits_error_or_degrade(
    sample_root: Path, sample_inputs
) -> None:
    tasks, tensors = sample_inputs
    task = tasks[0]
    subset = [task]
    subset_tensors = {task.case_id: tensors[task.case_id]}

    limited = RopeAttentionComponent(
        sample_root / "limited",
        limits=ResourceLimits(max_block_memory_bytes=1),
    )
    snapshot, _ = limited.commit_config(
        sample_configs()["v1"],
        source_name="limited",
        version=1,
    )
    with pytest.raises(RopeError) as captured:
        limited.compute(subset, subset_tensors, snapshot=snapshot)
    assert captured.value.code == E_RESOURCE_LIMIT

    degraded = RopeAttentionComponent(
        sample_root / "degraded",
        limits=ResourceLimits(max_cache_entries=3),
        tolerance=NumericTolerance(),
    )
    degraded_snapshot, _ = degraded.commit_config(
        sample_configs()["v1"],
        source_name="degraded",
        version=1,
    )
    result = degraded.compute(subset, subset_tensors, snapshot=degraded_snapshot)
    assert result.cache_diagnostics["entry_count"] <= 3
    assert result.degradations
    assert "OK_DEGRADED" in result.business_codes


def test_incremental_full_byte_mismatch_returns_inconsistent(
    sample_inputs,
) -> None:
    tasks, tensors = sample_inputs
    snapshot = ConfigSnapshot.create(
        parse_config(sample_configs()["v1"]),
        version=1,
        source_name="byte-check",
    )
    retargeted = retarget_tasks(tasks, snapshot.config)
    engine = AttentionEngine(None, use_cache=False)
    left = engine.compute_all(retargeted, tensors, snapshot)
    right = engine.compute_all(retargeted, tensors, snapshot)
    case_id = sorted(right.cases)[0]
    right.cases[case_id].output[0, 0, 0] += 1.0
    with pytest.raises(RopeError) as captured:
        assert_results_byte_identical(left, right)
    assert captured.value.code == "E_INCONSISTENT"

