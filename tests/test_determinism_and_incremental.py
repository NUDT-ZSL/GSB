from __future__ import annotations

import json

from ckptlab.derivation import build_checkpoint_artifacts
from ckptlab.plans import (
    content_digest,
    validate_shard_policy,
    validate_training_plan,
)

from conftest import manager_for, plan_v1, plan_v2, shard_policy


def test_repeated_generation_is_byte_identical():
    plan = validate_training_plan(plan_v1())
    policy = validate_shard_policy(shard_policy(), plan)
    first = build_checkpoint_artifacts(plan, policy)
    second = build_checkpoint_artifacts(plan, policy)
    assert content_digest(plan, policy) == content_digest(plan, policy)
    assert [shard.digest for shard in first.shards] == [
        shard.digest for shard in second.shards
    ]
    assert [shard.data for shard in first.shards] == [
        shard.data for shard in second.shards
    ]
    assert [layer.param_digest for layer in first.layers] == [
        layer.param_digest for layer in second.layers
    ]


def test_incremental_reuses_three_shards_and_matches_full_manifest(tmp_path):
    incremental_root = tmp_path / "incremental"
    full_root = tmp_path / "full"
    incremental = manager_for(incremental_root)
    v1 = incremental.save(plan_v1(), shard_policy())
    v1_objects = len(list((incremental_root / "store" / "objects").glob("*.shard")))
    v2 = incremental.save(plan_v2(), shard_policy())
    v2_objects = len(list((incremental_root / "store" / "objects").glob("*.shard")))

    assert v1["committed"] is True
    assert v2["summary"]["hit"] == 3
    assert v2["summary"]["written"] == 1
    assert v2_objects == v1_objects + 1
    assert {
        outcome["shard_id"]: outcome["reason"]
        for outcome in v2["outcomes"]
        if outcome["outcome"] == "hit"
    } == {
        "shard-0000": "unchanged_shard_digest",
        "shard-0001": "unchanged_shard_digest",
        "shard-0003": "unchanged_shard_digest",
    }

    full = manager_for(full_root)
    full_v2 = full.save(plan_v2(), shard_policy())
    assert full_v2["manifest_digest"] == v2["manifest_digest"]
    incremental_manifest = json.loads(
        (
            incremental_root
            / "store"
            / "manifests"
            / f"{v2['manifest_digest']}.json"
        ).read_text(encoding="utf-8")
    )
    full_manifest = json.loads(
        (full_root / "store" / "manifests" / f"{full_v2['manifest_digest']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert incremental_manifest == full_manifest


def test_repeated_same_version_is_idempotent(tmp_path):
    manager = manager_for(tmp_path)
    first = manager.save(plan_v1(), shard_policy())
    object_count = len(list((tmp_path / "store" / "objects").glob("*.shard")))
    second = manager.save(plan_v1(), shard_policy())
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["manifest_digest"] == first["manifest_digest"]
    assert len(list((tmp_path / "store" / "objects").glob("*.shard"))) == object_count
