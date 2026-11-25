from __future__ import annotations

import random

from gradient_engine.engine import ingest_event, run_to_completion
from gradient_engine.model import (
    GradientEvent,
    LayerSpec,
    LoadedInputs,
    NodeInfo,
    ShardSpec,
    SpecView,
)
from gradient_engine.storage import Store


def build_inputs() -> LoadedInputs:
    spec = SpecView(
        version=1,
        method="ring_test",
        layers=(
            LayerSpec(
                name="layer",
                parameter_bytes=100,
                shards=(ShardSpec(0, 40), ShardSpec(1, 60)),
            ),
        ),
    )
    nodes = (
        NodeInfo("n1", "127.0.0.1:6001", 1.0, 10_000),
        NodeInfo("n2", "127.0.0.1:6002", 1.0, 10_000),
    )
    events: list[GradientEvent] = []
    seq = 1
    for node in nodes:
        for shard in (0, 1):
            events.append(GradientEvent(
                event_seq=seq,
                logic_clock_ms=0,
                node_id=node.node_id,
                layer="layer",
                shard_index=shard,
                spec_version=1,
                expected_commit_ms=10,
                duration_ms=4,
            ))
            seq += 1
            events.append(GradientEvent(
                event_seq=seq,
                logic_clock_ms=1,
                node_id=node.node_id,
                layer="layer",
                shard_index=shard,
                spec_version=1,
                expected_commit_ms=10,
                duration_ms=4,
            ))
            seq += 1
    return LoadedInputs(
        plan={"scheduling": {"max_concurrency": 10, "time_budget_ms": 1000}},
        scheduling={"max_concurrency": 10, "time_budget_ms": 1000},
        initial_spec=spec,
        nodes=nodes,
        events=tuple(events),
        injections=(),
        paths={},
        raw_text={},
    )


def run_with_arrival_order(events_order: list[GradientEvent]) -> dict:
    inputs = build_inputs()
    store = Store.connect(":memory:")
    store.initialize(inputs)
    with store.conn:
        for event in events_order:
            ingest_event(store.conn, event, 0, 0)
    run_to_completion(store, inputs)
    status = store.status()
    trace = store.canonical_trace()
    store.close()
    return {"status": status["completion_sets"], "trace": trace}


def test_completion_sets_converge_for_shuffled_arrival_order() -> None:
    inputs = build_inputs()
    ordered = list(inputs.events)
    shuffled = list(inputs.events)
    random.Random(20261126).shuffle(shuffled)

    first = run_with_arrival_order(ordered)
    second = run_with_arrival_order(shuffled)

    assert first["status"] == second["status"]
    assert first["status"]["1"]["reduce_scatter"] == ["layer#0", "layer#1"]
    assert first["status"]["1"]["all_gather"] == ["layer#0", "layer#1"]


def test_duplicate_report_is_idempotent() -> None:
    inputs = build_inputs()
    store = Store.connect(":memory:")
    store.initialize(inputs)
    event = inputs.events[0]
    with store.conn:
        first = ingest_event(store.conn, event, 1, 0)
        second = ingest_event(store.conn, event, 2, 1)
    assert first.get("accepted") is True
    assert second.get("duplicate") is True
    holding_count = store.conn.execute("SELECT COUNT(*) AS n FROM holdings").fetchone()["n"]
    assert holding_count == 1
    report_count = store.conn.execute("SELECT COUNT(*) AS n FROM reports").fetchone()["n"]
    assert report_count == 1
    store.close()
