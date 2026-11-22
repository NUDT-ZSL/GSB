from __future__ import annotations

import pytest

from ckptlab.errors import BusinessError

from conftest import crash_plan, manager_for, plan_v1, shard_policy


def test_budget_exhaustion_marks_every_unstarted_shard(tmp_path):
    manager = manager_for(tmp_path)
    result = manager.save(plan_v1(logical_time_budget_ms=1), shard_policy())
    assert result["committed"] is False
    assert result["summary"]["written"] == 0
    assert result["summary"]["budget_unstarted"] == 4
    assert {item["reason"] for item in result["abandoned"]} == {
        "logical_budget_exhausted"
    }
    with pytest.raises(BusinessError) as caught:
        manager.restore()
    assert caught.value.code == "CHECKPOINT_NOT_FOUND"

    resumed = manager.resume(
        plan_v1(logical_time_budget_ms=1),
        shard_policy(),
        schedule_overrides={"logical_time_budget_ms": 200},
    )
    assert resumed["committed"] is True
    assert resumed["summary"]["written"] == 4


def test_started_failure_isolated_from_budget_unstarted(tmp_path):
    manager = manager_for(tmp_path)
    crash = crash_plan(
        enabled=True,
        point="none",
        fail_shards=["shard-0000"],
    )
    result = manager.save(
        plan_v1(max_concurrency=2), shard_policy(), crash
    )
    assert result["committed"] is False
    assert result["status"] == "failed"
    assert result["summary"]["failed_after_start"] == 1
    assert result["summary"]["written"] == 1
    assert result["summary"]["not_started"] == 2
    assert result["summary"]["budget_unstarted"] == 0
    by_shard = {item["shard_id"]: item for item in result["outcomes"]}
    assert by_shard["shard-0000"]["outcome"] == "failed"
    assert by_shard["shard-0001"]["outcome"] == "written"
    assert by_shard["shard-0002"]["started"] is False
    assert by_shard["shard-0003"]["started"] is False

    resumed = manager.resume(plan_v1(max_concurrency=2), shard_policy())
    assert resumed["committed"] is True


def test_operator_cancel_is_a_distinct_outcome(tmp_path):
    manager = manager_for(tmp_path)
    crash = crash_plan(enabled=True, point="none", cancel_after_started=1)
    result = manager.save(
        plan_v1(max_concurrency=1), shard_policy(), crash
    )
    assert result["committed"] is False
    assert result["summary"]["written"] == 1
    assert result["summary"]["actively_abandoned"] == 3
    assert result["summary"]["failed_after_start"] == 0
    assert {item["reason"] for item in result["abandoned"]} == {
        "operator_cancelled"
    }

    resumed = manager.resume(plan_v1(max_concurrency=1), shard_policy())
    assert resumed["committed"] is True
