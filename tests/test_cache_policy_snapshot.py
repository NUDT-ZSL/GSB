import copy
import json

import pytest

from opchain.cache import (
    ContentCache,
    build_cache_key,
    stage_cache_parts,
)
from opchain.config import Config
from opchain.engine import execute_case, run_batch
from opchain.errors import ErrorCode, OpChainError
from opchain.hashing import hash_array
from opchain.policy import adjudicate_block_size
from opchain.samplegen import VALID_CASES
from opchain.selfcheck import run_full_selfcheck
from opchain.snapshot import read_snapshot, replace_snapshot
from opchain.task import generate_input, parse_spec


def test_cache_key_covers_all_required_components(specs):
    spec = specs[0]
    tensor = generate_input(spec)
    input_hash = hash_array(tensor)
    parts = stage_cache_parts(spec, 1, input_hash, 32, "float64")
    key = build_cache_key(parts)

    changed = copy.deepcopy(parts)
    changed["block_size"] = 33
    assert build_cache_key(changed) != key

    changed = copy.deepcopy(parts)
    changed["eps"] = parts["eps"] + 1e-12
    assert build_cache_key(changed) != key

    changed = copy.deepcopy(parts)
    changed["precision"] = "float32"
    assert build_cache_key(changed) != key


def test_suffix_invalidation_reuses_prefix(specs, config):
    spec = specs[0]
    tensor = generate_input(spec)
    input_hash = hash_array(tensor)
    old_keys = [
        build_cache_key(stage_cache_parts(spec, stage, input_hash, 32, "float64"))
        for stage in range(len(spec.op_chain))
    ]
    changed = copy.deepcopy(spec)
    changed.block_size = 64
    new_keys = [
        build_cache_key(stage_cache_parts(changed, stage, input_hash, 64, "float64"))
        for stage in range(len(changed.op_chain))
    ]
    cache = ContentCache()
    plan = cache.plan_invalidation(
        spec.case_id, spec, changed, old_keys, new_keys
    )
    assert plan["reused_count"] == 0
    assert plan["invalidated_count"] == len(old_keys)
    assert not set(plan["reused_keys"]) & set(plan["invalidated_keys"])
    assert set(plan["affected_keys"]) == set(plan["reused_keys"]) | set(
        plan["invalidated_keys"]
    )


def test_activation_change_invalidates_suffix_only(specs):
    spec = specs[0]
    tensor = generate_input(spec)
    input_hash = hash_array(tensor)
    old_keys = [
        build_cache_key(stage_cache_parts(spec, stage, input_hash, 32, "float64"))
        for stage in range(len(spec.op_chain))
    ]
    changed = copy.deepcopy(spec)
    # Only the last activation kind changes; prefix keys stay identical.
    changed.op_chain[-1] = {"op": "relu"}
    new_keys = [
        build_cache_key(stage_cache_parts(changed, stage, input_hash, 32, "float64"))
        for stage in range(len(changed.op_chain))
    ]
    plan = ContentCache().plan_invalidation(
        spec.case_id, spec, changed, old_keys, new_keys
    )
    assert plan["reused_count"] == len(spec.op_chain) - 1
    assert plan["invalidated_count"] == 1


def test_policy_resource_limit_when_degrade_disabled():
    record = copy.deepcopy(VALID_CASES[0])
    record["input_shape"] = [512, 512]
    record["block_size"] = 1024
    record["policy"] = {"max_resident_bytes": 1024, "allow_degrade": False}
    spec = parse_spec(record)
    with pytest.raises(OpChainError) as exc:
        adjudicate_block_size(spec, Config().with_overrides(spec.policy))
    assert exc.value.code == ErrorCode.RESOURCE_LIMIT


def test_policy_records_rationale_and_selection(specs, config):
    plan = adjudicate_block_size(specs[0], config)
    assert plan["selected_block_size"] == specs[0].block_size
    assert plan["priority"] == list(config.priority)
    assert plan["rationale"]


def test_snapshot_roundtrip_and_corruption_detection(specs, config, tmp_path):
    envelope, _, _ = run_batch(specs, config)
    path = tmp_path / "snapshot.json"
    replace_snapshot(path, envelope["kind"], envelope["payload"])
    loaded = read_snapshot(path)
    assert loaded["checksum"] == envelope["checksum"]

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["payload"]["results"][0]["final_hash"] = "tampered"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(OpChainError) as exc:
        read_snapshot(path)
    assert exc.value.code == ErrorCode.SNAPSHOT_CORRUPT


def test_snapshot_versions_increase_on_replace(specs, config, tmp_path):
    path = tmp_path / "snapshot.json"
    first = replace_snapshot(path, "opchain-snapshot", {"a": 1})
    second = replace_snapshot(
        path, "opchain-snapshot", {"a": 2}, previous_version=first["version"]
    )
    assert second["version"] == first["version"] + 1


def test_full_selfcheck_passes(specs, config):
    envelope, _, _ = run_batch(specs, config)
    report = run_full_selfcheck(specs, envelope, config)
    assert report["code"] == ErrorCode.OK
    assert report["cache_key_check"]["checked_entries"] > 0
    assert len(report["replay_check"]) == len(specs)


def test_parallel_recompute_ceiling_enforced():
    cache = ContentCache()
    plan = {
        "reused_keys": [],
        "invalidated_keys": [f"k{index}" for index in range(5)],
        "affected_keys": [f"k{index}" for index in range(5)],
    }
    with pytest.raises(OpChainError) as exc:
        cache.enforce_recompute_limits(
            plan, max_cache_entries=100, max_parallel_recompute=4
        )
    assert exc.value.code == ErrorCode.RESOURCE_LIMIT


def test_cache_entry_ceiling_enforced():
    cache = ContentCache()
    plan = {
        "reused_keys": [],
        "invalidated_keys": ["k1"],
        "affected_keys": ["k1"],
    }
    with pytest.raises(OpChainError) as exc:
        cache.enforce_recompute_limits(
            plan, max_cache_entries=0, max_parallel_recompute=4
        )
    assert exc.value.code == ErrorCode.RESOURCE_LIMIT
