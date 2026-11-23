from __future__ import annotations

from pathlib import Path

import pytest

from coldstart_ledger.engine import STAGE_READING, STAGE_WITHDRAWN
from coldstart_ledger.errors import BusinessError, ErrorCode

from conftest import make_engine


def _layers():
    return [
        {"digest": "sha256:low", "bytes": 1000, "node": "node-a", "ref_count": 1},
        {"digest": "sha256:high", "bytes": 2000, "node": "node-a", "ref_count": 1},
    ]


def _instances():
    return [
        {
            "instance_id": "low",
            "service": "svc",
            "priority": 10,
            "requested_ephemeral_bytes": 6000,
            "required_layers": ["sha256:low"],
            "deadline_ms": 1000,
        },
        {
            "instance_id": "high",
            "service": "svc",
            "priority": 90,
            "requested_ephemeral_bytes": 4000,
            "required_layers": ["sha256:high"],
            "deadline_ms": 1000,
        },
    ]


def _node(available: int = 10_000):
    return [
        {
            "node": "node-a",
            "available_ephemeral_bytes": available,
            "image_layer_bytes": 3000,
            "max_concurrent_cold_starts": 1,
        }
    ]


def test_preemption_plan_and_atomic_apply_with_unsettled_refund(tmp_path: Path) -> None:
    engine = make_engine(
        tmp_path,
        instances=_instances(),
        layers=_layers(),
        nodes=_node(),
    )
    engine.admit("low", "r1", 1)
    plan = engine.plan_preemption("high", 2)
    assert plan["can_admit_after"] is True
    assert [item["instance_id"] for item in plan["reclaim_chain"]] == ["low"]
    assert plan["node_available_after"] >= plan["requested_ephemeral_bytes"]
    assert plan["reclaim_chain"][0]["reason"]["priority_lower"] == "10 < 90"
    assert plan["node_available_before"] >= 0

    applied = engine.admit("high", "r2", 2)
    assert applied["stage"] == STAGE_READING
    state = engine.snapshot()["state"]
    low = state["instances"]["low"]
    assert low["stage"] == STAGE_WITHDRAWN
    assert low["outcome"] == "PREEMPTED"
    assert low["refund_ids"] == ["refund-00000001"]
    assert state["refunds"][0]["status"] == "UNSETTLED"
    assert state["refunds"][0]["consumed_read_ticks"] == low["read_ticks"]
    assert state["nodes"]["node-a"]["available_ephemeral_bytes"] >= 0
    assert engine.audit()["ok"] is True

    settled = engine.settle_refund("refund-00000001", "r3", 3)
    assert settled["status"] == "SETTLED"
    assert engine.snapshot()["state"]["refunds"][0]["status"] == "SETTLED"


def test_failed_preemption_rolls_back_without_partial_charge(tmp_path: Path) -> None:
    instances = _instances()
    instances[1]["requested_ephemeral_bytes"] = 7000
    engine = make_engine(
        tmp_path,
        instances=instances,
        layers=_layers(),
        nodes=_node(available=6000),
    )
    engine.admit("low", "r1", 1)
    before = engine.snapshot()
    plan = engine.plan_preemption("high", 2)
    assert plan["can_admit_after"] is False
    assert plan["shortfall_bytes"] > 0
    with pytest.raises(BusinessError) as caught:
        engine.admit("high", "r2", 2)
    assert caught.value.code == ErrorCode.NO_CAPACITY
    after = engine.snapshot()
    assert after["state_sha256"] == before["state_sha256"]
    assert after["state"]["instances"]["low"]["stage"] == STAGE_READING
    assert after["state"]["refunds"] == []


def test_same_or_higher_priority_instance_is_not_preempted(tmp_path: Path) -> None:
    instances = _instances()
    instances[0]["priority"] = 90
    engine = make_engine(
        tmp_path,
        instances=instances,
        layers=_layers(),
        nodes=_node(available=6000),
    )
    engine.admit("low", "r1", 1)
    plan = engine.plan_preemption("high", 2)
    assert plan["reclaim_chain"] == []
    assert plan["can_admit_after"] is False


def test_multi_victim_chain_frees_storage_and_slots_atomically(tmp_path: Path) -> None:
    layers = [
        {"digest": "sha256:a", "bytes": 3000, "node": "node-a", "ref_count": 1},
        {"digest": "sha256:b", "bytes": 3000, "node": "node-a", "ref_count": 1},
        {"digest": "sha256:c", "bytes": 4000, "node": "node-a", "ref_count": 1},
    ]
    instances = [
        {
            "instance_id": "a",
            "service": "svc",
            "priority": 10,
            "requested_ephemeral_bytes": 3000,
            "required_layers": ["sha256:a"],
            "deadline_ms": 1000,
        },
        {
            "instance_id": "b",
            "service": "svc",
            "priority": 20,
            "requested_ephemeral_bytes": 3000,
            "required_layers": ["sha256:b"],
            "deadline_ms": 1000,
        },
        {
            "instance_id": "c",
            "service": "svc",
            "priority": 90,
            "requested_ephemeral_bytes": 5000,
            "required_layers": ["sha256:c"],
            "deadline_ms": 1000,
        },
    ]
    nodes = [
        {
            "node": "node-a",
            "available_ephemeral_bytes": 6000,
            "image_layer_bytes": 10_000,
            "max_concurrent_cold_starts": 2,
        }
    ]
    engine = make_engine(
        tmp_path,
        instances=instances,
        layers=layers,
        nodes=nodes,
    )
    engine.admit("a", "r1", 1)
    engine.complete_reading("a", "r1", ["sha256:a"], 2)
    engine.admit("b", "r2", 3)
    plan = engine.plan_preemption("c", 4)
    assert plan["can_admit_after"] is True
    assert [item["instance_id"] for item in plan["reclaim_chain"]] == ["a", "b"]
    assert plan["freed_ephemeral_bytes"] == 6000
    assert plan["freed_active_slots"] == 2
    result = engine.admit("c", "r3", 4)
    assert result["stage"] == STAGE_READING
    state = engine.snapshot()["state"]
    assert len(state["refunds"]) == 2
    assert all(refund["status"] == "UNSETTLED" for refund in state["refunds"])
    assert state["nodes"]["node-a"]["active_count"] == 1
    assert state["nodes"]["node-a"]["charged_ephemeral_bytes"] == 5000
    assert engine.audit()["ok"] is True

