from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from transformer_fusion import cache_diagnose, execute
from transformer_fusion.cache import fused_cache_key
from transformer_fusion.generate_data import demo_cases


def test_content_addressed_cache_key_sensitivity():
    task = demo_cases()[2]
    key = fused_cache_key(task, "input-hash", effective_block_size=4)
    assert key == fused_cache_key(task, "input-hash", effective_block_size=4)
    assert key != fused_cache_key(task.evolved(eps=task.eps * 10), "input-hash", effective_block_size=4)
    assert key != fused_cache_key(
        task.evolved(axis_order=(0, 1)), "input-hash", effective_block_size=4
    )
    assert key != fused_cache_key(task, "input-hash", effective_block_size=5)
    assert key != fused_cache_key(
        task, "input-hash", effective_block_size=4, effective_dtype="float32"
    )


def test_hot_cache_reuse_and_partial_invalidation(dataset, stores):
    data_dir, manifest = dataset
    first = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=stores["cache_dir"],
        snapshot_dir=stores["snapshot_dir"],
    )
    assert first.ok
    fused_first = next(case for case in first.cases if case.plan.path == "fused")
    assert fused_first.cache["hit"] is False

    second = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=stores["cache_dir"],
        snapshot_dir=stores["snapshot_dir"],
    )
    assert second.ok
    fused_second = next(case for case in second.cases if case.plan.path == "fused")
    assert fused_second.cache["hit"] is True
    assert fused_second.cache["key"] == fused_first.cache["key"]

    diagnosis = cache_diagnose(
        manifest,
        changes={
            "case_silu_eps_axis": {"eps": 1e-3},
            "case_gelu_perm": {"axis_order": [1, 2, 0]},
            "case_downstream_layernorm": {"axis_order": [1, 2, 0]},
        },
        data_dir=data_dir,
        cache_dir=stores["cache_dir"],
        snapshot_dir=stores["snapshot_dir"],
    )
    assert diagnosis["ok"]
    assert diagnosis["no_intersection"]
    assert diagnosis["union_covers_all_affected_nodes"]
    assert set(diagnosis["affected_cases"]) == {
        "case_silu_eps_axis",
        "case_gelu_perm",
        "case_downstream_layernorm",
    }
    assert "axis_mapping" in diagnosis["reused_nodes"]["case_silu_eps_axis"]
    assert "activation" in diagnosis["invalidated_nodes"]["case_silu_eps_axis"]
    assert "activation" in diagnosis["reused_nodes"]["case_gelu_perm"]
    assert "axis_mapping" in diagnosis["invalidated_nodes"]["case_gelu_perm"]
    assert "downstream:2:layernorm" in diagnosis["invalidated_nodes"][
        "case_downstream_layernorm"
    ]
    assert diagnosis["recomputed_cases"] == sorted(diagnosis["affected_cases"])
    assert diagnosis["cache_hit_cases"]

    for case_id in diagnosis["affected_cases"]:
        invalid = set(diagnosis["invalidated_nodes"][case_id])
        reused = set(diagnosis["reused_nodes"][case_id])
        all_nodes = invalid | reused
        assert not (invalid & reused)
        assert all_nodes
        assert set(diagnosis["recompute_suffix"][case_id]) <= invalid


def test_cache_artifact_and_entry_are_verifiable(dataset, stores):
    data_dir, manifest = dataset
    report = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=stores["cache_dir"],
        snapshot_dir=stores["snapshot_dir"],
    )
    index = json.loads((stores["cache_dir"] / "index.json").read_text(encoding="utf-8"))
    assert index["version"] >= 1
    assert index["checksum"]
    for entry in index["entries"].values():
        assert entry["version"] >= 1
        assert entry["checksum"]
        artifact = stores["cache_dir"] / entry["artifact"]
        assert artifact.exists()
        import hashlib

        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == entry["artifact_sha256"]
    for case in report.cases:
        if case.plan.fuse:
            assert case.cache["version"] >= 1
            assert case.cache["checksum"]
            assert case.cache["artifact_sha256"]
