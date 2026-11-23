from __future__ import annotations

import json
from pathlib import Path

import pytest

from coldstart_ledger.engine import (
    STAGE_PENDING,
    STAGE_READING,
    STAGE_WITHDRAWN,
    STAGE_WRITING,
    ColdStartEngine,
)
from coldstart_ledger.errors import BusinessError, ErrorCode
from coldstart_ledger.fixed import convert_floor, decimal_ticks

from conftest import make_engine


def _instances(count: int, *, deadline: int = 1000, priority: int = 10):
    return [
        {
            "instance_id": f"i{index}",
            "service": "svc",
            "priority": priority,
            "requested_ephemeral_bytes": 1000,
            "required_layers": [f"sha256:l{index}"],
            "deadline_ms": deadline,
        }
        for index in range(1, count + 1)
    ]


def _layers(count: int, *, layer_bytes: int = 1, node: str = "node-a"):
    return [
        {
            "digest": f"sha256:l{index}",
            "bytes": layer_bytes,
            "node": node,
            "ref_count": 2,
        }
        for index in range(1, count + 1)
    ]


def _node(
    *,
    available: int = 100_000,
    image_bytes: int = 100_000,
    concurrency: int = 4,
    name: str = "node-a",
):
    return [
        {
            "node": name,
            "available_ephemeral_bytes": available,
            "image_layer_bytes": image_bytes,
            "max_concurrent_cold_starts": concurrency,
        }
    ]


def test_fixed_conversion_carry_is_exact_and_repeatable() -> None:
    first = convert_floor(1000, 3, 1000, 0)
    second = convert_floor(1000, 3, 1000, first.remainder)
    assert (first.ticks, first.remainder) == (333_333, 1)
    assert (second.ticks, second.carry_in, second.remainder) == (333_333, 1, 2)
    assert first.direction == "floor"
    assert decimal_ticks(1000, 3, 1000) == decimal_ticks(1000, 3, 1000)
    assert convert_floor(1000, 3, 1000, 0) == first


def test_instance_and_node_residuals_participate_separately(tmp_path: Path) -> None:
    engine = make_engine(
        tmp_path,
        instances=_instances(3),
        layers=_layers(3, layer_bytes=1),
        nodes=_node(concurrency=3),
        config={"layer_read_bandwidth_bytes_per_second": 3, "round_logical_time_budget_ms": 10000},
    )
    for index in range(1, 4):
        result = engine.admit(f"i{index}", f"r{index}", index)
        assert result["stage"] == STAGE_READING
    state = engine.snapshot()["state"]
    instance_rows = [
        row
        for row in state["conversions"]
        if row["metric"] == "layer_read_ticks" and row["scope_type"] == "instance"
    ]
    node_rows = [
        row
        for row in state["conversions"]
        if row["metric"] == "layer_read_ticks" and row["scope_type"] == "node"
    ]
    assert [row["quotient"] for row in instance_rows] == [333_333, 333_333, 333_333]
    assert [row["remainder"] for row in instance_rows] == [1, 1, 1]
    assert [row["quotient"] for row in node_rows] == [333_333, 333_333, 333_334]
    assert [row["remainder"] for row in node_rows] == [1, 2, 0]
    assert state["residuals"]["node:node-a:layer_read_ticks:1000"] == 0
    assert sum(row["quotient"] for row in node_rows) == 1_000_000
    assert engine.audit()["ok"] is True

    replay = make_engine(
        tmp_path / "replay",
        instances=_instances(3),
        layers=_layers(3, layer_bytes=1),
        nodes=_node(concurrency=3),
        config={"layer_read_bandwidth_bytes_per_second": 3, "round_logical_time_budget_ms": 10000},
    )
    for index in range(1, 4):
        replay.admit(f"i{index}", f"r{index}", index)
    assert engine.snapshot()["state_sha256"] == replay.snapshot()["state_sha256"]


def test_deadline_is_checked_before_budget_and_does_not_enter_reading(tmp_path: Path) -> None:
    engine = make_engine(
        tmp_path,
        instances=[
            {
                "instance_id": "i1",
                "service": "svc",
                "priority": 1,
                "requested_ephemeral_bytes": 1000,
                "required_layers": ["sha256:l1"],
                "deadline_ms": 5,
            }
        ],
        layers=_layers(1, layer_bytes=1000),
        config={"round_logical_time_budget_ms": 1},
    )
    result = engine.run_round("r1", 6).to_dict()
    state = engine.snapshot()["state"]
    instance = state["instances"]["i1"]
    assert instance["stage"] == STAGE_WITHDRAWN
    assert instance["outcome"] == "DEADLINE_MISSED"
    assert instance["reserved_write_bytes"] == 0
    assert state["nodes"]["node-a"]["charged_ephemeral_bytes"] == 0
    assert result["summary"]["abandoned"] == []
    assert result["summary"]["not_started_within_budget"] == []


def test_layer_mismatch_missing_and_success_have_distinct_codes(tmp_path: Path) -> None:
    engine = make_engine(
        tmp_path,
        instances=_instances(1),
        layers=_layers(1, layer_bytes=1000),
    )
    engine.admit("i1", "r1", 1)
    with pytest.raises(BusinessError) as missing:
        engine.complete_reading("i1", "r2", [], 2)
    assert missing.value.code == ErrorCode.LAYER_MISSING
    with pytest.raises(BusinessError) as mismatch:
        engine.complete_reading("i1", "r3", ["sha256:l1", "sha256:other"], 3)
    assert mismatch.value.code == ErrorCode.LAYER_DIGEST_MISMATCH
    state = engine.snapshot()["state"]
    assert state["instances"]["i1"]["stage"] == STAGE_READING
    result = engine.complete_reading("i1", "r4", ["sha256:l1"], 4)
    assert result["stage"] == STAGE_WRITING
    assert engine.complete_writing("i1", "r5", 5)["stage"] == "READY"
    assert engine.audit()["ok"] is True


def test_withdrawal_and_read_failure_use_distinct_outcomes(tmp_path: Path) -> None:
    engine = make_engine(
        tmp_path,
        instances=[
            {
                "instance_id": "deadline",
                "service": "svc",
                "priority": 1,
                "requested_ephemeral_bytes": 10,
                "required_layers": ["sha256:l1"],
                "deadline_ms": 1,
            },
            {
                "instance_id": "failure",
                "service": "svc",
                "priority": 1,
                "requested_ephemeral_bytes": 10,
                "required_layers": ["sha256:l1"],
                "deadline_ms": 100,
            },
        ],
        layers=_layers(1, layer_bytes=1000),
        nodes=_node(concurrency=2),
    )
    deadline = engine.admit("deadline", "r1", 2)
    assert deadline["outcome"] == "DEADLINE_MISSED"
    engine.admit("failure", "r2", 3)
    failed = engine.fail_instance("failure", "r3", "READ_IO", 4)
    assert failed["outcome"] == "READ_LAYER_FAILED"
    assert failed["outcome_category"] == "FAILED_AFTER_START"
    assert failed["outcome"] != deadline["outcome"]
    assert engine.audit()["ok"] is True


def test_clock_rollback_has_dedicated_code_and_rolls_back(tmp_path: Path) -> None:
    engine = make_engine(tmp_path)
    engine.admit("i1", "r1", 10)
    before = engine.snapshot()
    with pytest.raises(BusinessError) as caught:
        engine.complete_reading("i1", "r2", ["sha256:l1"], 9)
    assert caught.value.code == ErrorCode.CLOCK_ROLLBACK
    after = engine.snapshot()
    assert after["state_sha256"] == before["state_sha256"]
    assert after["state"]["instances"]["i1"]["stage"] == STAGE_READING


def test_clock_rollback_within_threshold_is_accepted(tmp_path: Path) -> None:
    engine = make_engine(
        tmp_path,
        config={"clock_rollback_threshold_ms": 10},
    )
    engine.admit("i1", "r1", 100)
    state = engine.snapshot()["state"]
    assert state["clock_watermark_ms"] == 100
    result = engine.complete_reading("i1", "r2", ["sha256:l1"], 95)
    assert result["stage"] == STAGE_WRITING
    state = engine.snapshot()["state"]
    assert state["last_injected_clock_ms"] == 95
    assert state["clock_watermark_ms"] == 100


def test_layer_byte_change_withdraws_inflight_instances(tmp_path: Path) -> None:
    engine = make_engine(
        tmp_path,
        instances=_instances(2),
        layers=_layers(2, layer_bytes=1000),
        nodes=_node(concurrency=2, image_bytes=2000),
    )
    engine.admit("i1", "r1", 1)
    before = engine.snapshot()["state"]
    assert before["nodes"]["node-a"]["charged_ephemeral_bytes"] == 1000
    result = engine.apply_layer_change("sha256:l1", 2000, "r2", 2)
    assert result["withdrawn_instances"] == ["i1"]
    state = engine.snapshot()["state"]
    assert state["instances"]["i1"]["stage"] == STAGE_WITHDRAWN
    assert state["instances"]["i1"]["outcome"] == "LAYER_CHANGED"
    assert state["nodes"]["node-a"]["charged_ephemeral_bytes"] == 0
    assert state["nodes"]["node-a"]["image_layer_bytes"] == 3000
    assert state["layers"]["sha256:l1"]["bytes"] == 2000
    assert engine.audit()["ok"] is True
    with pytest.raises(BusinessError) as caught:
        engine.apply_layer_change("sha256:missing", 1, "r3", 3)
    assert caught.value.code == ErrorCode.LAYER_MISSING


def test_pending_outcome_does_not_change_stage(tmp_path: Path) -> None:
    engine = make_engine(
        tmp_path,
        instances=_instances(2),
        layers=_layers(2, layer_bytes=1000),
        nodes=_node(concurrency=1),
    )
    result = engine.run_round("r1", 1).to_dict()
    state = engine.snapshot()["state"]
    assert result["summary"]["not_started_within_budget"] == ["i2"]
    assert state["instances"]["i2"]["stage"] == STAGE_PENDING
    assert state["instances"]["i2"]["outcome"] == "NOT_STARTED_CAPACITY"

