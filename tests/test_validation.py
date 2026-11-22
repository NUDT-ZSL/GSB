from __future__ import annotations

import copy

import pytest

from ckptlab.errors import BusinessError

from conftest import db_rows, manager_for, plan_v1, plan_v2, shard_policy


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda plan, policy: plan["layers"][1].update(
                name=plan["layers"][0]["name"]
            ),
            "LAYER_NAME_DUPLICATE",
        ),
        (
            lambda plan, policy: plan["layers"][0].update(shape=[2, 0]),
            "LAYER_SHAPE_INVALID",
        ),
        (
            lambda plan, policy: plan["layers"][0].update(dtype="complex128"),
            "DTYPE_INVALID",
        ),
        (
            lambda plan, policy: policy.update(num_shards=99),
            "SHARD_COUNT_INVALID",
        ),
        (
            lambda plan, policy: plan["save"].update(max_concurrency=0),
            "CONCURRENCY_INVALID",
        ),
        (
            lambda plan, policy: plan["save"].update(logical_time_budget_ms=0),
            "BUDGET_INVALID",
        ),
        (
            lambda plan, policy: policy.update(max_shard_bytes=1),
            "SHARD_SIZE_EXCEEDED",
        ),
    ],
)
def test_invalid_plan_has_code_and_no_persistence(tmp_path, mutate, code):
    plan, policy = plan_v1(), shard_policy()
    mutate(plan, policy)
    manager = manager_for(tmp_path)
    with pytest.raises(BusinessError) as caught:
        manager.save(plan, policy)
    assert caught.value.code == code
    assert not (tmp_path / "ledger.sqlite3").exists()
    assert not (tmp_path / "store").exists()


def test_malformed_crash_plan_does_not_write(tmp_path):
    plan, policy = plan_v1(), shard_policy()
    manager = manager_for(tmp_path)
    with pytest.raises(BusinessError) as caught:
        manager.save(plan, policy, {"enabled": True, "point": "explode"})
    assert caught.value.code == "CRASH_PLAN_INVALID"
    assert not tmp_path.joinpath("ledger.sqlite3").exists()
    assert not tmp_path.joinpath("store").exists()


def test_version_rollback_and_clock_rollback_do_not_create_builds(tmp_path):
    manager = manager_for(tmp_path)
    manager.save(plan_v1(), shard_policy())
    manager.save(plan_v2(), shard_policy())
    before = db_rows(tmp_path / "ledger.sqlite3", "SELECT * FROM builds ORDER BY version")

    old = plan_v1(logical_clock_ms=5000)
    with pytest.raises(BusinessError) as caught:
        manager.save(old, shard_policy())
    assert caught.value.code == "VERSION_ROLLBACK"

    rolled_back_clock = plan_v2(logical_clock_ms=0, version=3)
    with pytest.raises(BusinessError) as caught:
        manager.save(rolled_back_clock, shard_policy())
    assert caught.value.code == "CLOCK_ROLLBACK"
    after = db_rows(tmp_path / "ledger.sqlite3", "SELECT * FROM builds ORDER BY version")
    assert after == before


def test_same_version_different_content_conflicts(tmp_path):
    manager = manager_for(tmp_path)
    manager.save(plan_v1(), shard_policy())
    conflict = plan_v1(logical_clock_ms=3000)
    conflict["seed"] += 1
    with pytest.raises(BusinessError) as caught:
        manager.save(conflict, shard_policy())
    assert caught.value.code == "VERSION_CONFLICT"
    assert len(db_rows(tmp_path / "ledger.sqlite3", "SELECT * FROM builds")) == 1
    assert manager.status()["pointer"]["version"] == 1
