from __future__ import annotations

from pathlib import Path

from coldstart_ledger.engine import STAGE_READING

from conftest import make_engine


def _instances(count: int):
    return [
        {
            "instance_id": f"i{index}",
            "service": "svc",
            "priority": 100 - index,
            "requested_ephemeral_bytes": 1000,
            "required_layers": [f"sha256:l{index}"],
            "deadline_ms": 1000,
        }
        for index in range(1, count + 1)
    ]


def _layers(count: int, layer_bytes: int = 3000):
    return [
        {
            "digest": f"sha256:l{index}",
            "bytes": layer_bytes,
            "node": "node-a",
            "ref_count": 2,
        }
        for index in range(1, count + 1)
    ]


def _nodes(concurrency: int):
    return [
        {
            "node": "node-a",
            "available_ephemeral_bytes": 100_000,
            "image_layer_bytes": 20_000,
            "max_concurrent_cold_starts": concurrency,
        }
    ]


def test_round_budget_abandons_remaining_and_leaves_started_active(tmp_path: Path) -> None:
    engine = make_engine(
        tmp_path,
        instances=_instances(3),
        layers=_layers(3, layer_bytes=3000),
        nodes=_nodes(1),
        config={"round_logical_time_budget_ms": 5},
    )
    result = engine.run_round("r1", 1).to_dict()
    state = engine.snapshot()["state"]
    assert result["summary"]["admitted"] == ["i1"]
    assert result["summary"]["not_started_within_budget"] == ["i2", "i3"]
    assert state["instances"]["i1"]["stage"] == STAGE_READING
    assert state["instances"]["i2"]["stage"] == "PENDING_APPROVAL"
    assert state["instances"]["i2"]["outcome"] == "NOT_STARTED_BUDGET"
    assert all(
        row["outcome_category"] != "FAILED_AFTER_START" for row in result["outcomes"]
    )


def test_node_concurrency_strictly_limits_parallel_starts(tmp_path: Path) -> None:
    engine = make_engine(
        tmp_path,
        instances=_instances(3),
        layers=_layers(3, layer_bytes=1000),
        nodes=_nodes(2),
        config={"round_logical_time_budget_ms": 100},
    )
    result = engine.run_round("r1", 1).to_dict()
    state = engine.snapshot()["state"]
    assert len(result["summary"]["admitted"]) == 2
    assert state["nodes"]["node-a"]["active_count"] == 2
    assert state["nodes"]["node-a"]["active_count"] <= state["nodes"]["node-a"][
        "max_concurrent_cold_starts"
    ]
    assert result["summary"]["not_started_within_budget"] == ["i3"]
    assert engine.audit()["ok"] is True


def test_round_result_is_idempotent_for_same_round_source(tmp_path: Path) -> None:
    engine = make_engine(
        tmp_path,
        instances=_instances(2),
        layers=_layers(2, layer_bytes=1000),
        nodes=_nodes(1),
    )
    first = engine.run_round("r1", 1)
    second = engine.run_round("r1", 1)
    assert first.revision == second.revision
    assert second.applied is False
    assert second.to_dict()["idempotent_replay"] is True
    state = engine.snapshot()["state"]
    assert len(state["rounds"]) == 1
    assert len(state["conversions"]) == 4


def test_multiple_nodes_are_scheduled_in_independent_groups(tmp_path: Path) -> None:
    layers = [
        {"digest": "sha256:a", "bytes": 1000, "node": "node-a", "ref_count": 1},
        {"digest": "sha256:b", "bytes": 1000, "node": "node-b", "ref_count": 1},
    ]
    nodes = [
        {
            "node": "node-a",
            "available_ephemeral_bytes": 10_000,
            "image_layer_bytes": 1000,
            "max_concurrent_cold_starts": 1,
        },
        {
            "node": "node-b",
            "available_ephemeral_bytes": 10_000,
            "image_layer_bytes": 1000,
            "max_concurrent_cold_starts": 1,
        },
    ]
    instances = [
        {
            "instance_id": "a",
            "service": "svc",
            "priority": 10,
            "requested_ephemeral_bytes": 1000,
            "required_layers": ["sha256:a"],
            "deadline_ms": 100,
        },
        {
            "instance_id": "b",
            "service": "svc",
            "priority": 10,
            "requested_ephemeral_bytes": 1000,
            "required_layers": ["sha256:b"],
            "deadline_ms": 100,
        },
    ]
    engine = make_engine(tmp_path, instances=instances, layers=layers, nodes=nodes)
    result = engine.run_round("r1", 1).to_dict()
    assert result["summary"]["admitted"] == ["a", "b"]
    assert len(result["schedule_by_node"]["node-a"]["admitted"]) == 1
    assert len(result["schedule_by_node"]["node-b"]["admitted"]) == 1

