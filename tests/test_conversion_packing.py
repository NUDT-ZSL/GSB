from __future__ import annotations

from decimal import Decimal

from scaler.conversion import convert_target
from scaler.models import NodeSpec, Observation
from scaler.packing import choose_placement, make_initial_ledger


def test_conversion_is_bitwise_stable_and_records_evidence(base_spec) -> None:
    service = base_spec.services["chat-alpha"]
    observation = Observation(0, 1000, 2.0, 512.0)
    outputs = [convert_target(service, observation, 1000).to_dict() for _ in range(100)]
    assert all(item == outputs[0] for item in outputs)
    result = outputs[0]
    assert result["target_replicas"] == 2
    evidence = result["evidence"]
    assert evidence["demand_tokens_per_second"] == "1024.00"
    assert evidence["replica_capacity_tokens_per_second"] == "600.0"
    assert evidence["ceil_result"] == 2
    assert evidence["ceil_applied_to"] == "post_division_ratio"
    assert evidence["aggregation_order"][-1].startswith("clamp")


def test_conversion_clamps_min_and_max(base_spec) -> None:
    service = base_spec.services["chat-alpha"]
    low = convert_target(service, Observation(0, 1000, 0.0, 0.0), 1000)
    assert low.target_replicas == service.min_replicas
    high = convert_target(service, Observation(0, 1000, 1000.0, 4096.0), 1000)
    assert high.target_replicas == service.max_replicas
    assert Decimal(high.evidence["raw_ratio"]) > Decimal(service.max_replicas)


def test_packing_enforces_both_budgets_and_is_idempotent(base_spec) -> None:
    service = base_spec.services["chat-alpha"]
    nodes = {
        "n1": NodeSpec("n1", 2, 8192, 2, "zone-a"),
        "n2": NodeSpec("n2", 4, 8192, 4, "zone-b"),
    }
    ledger = make_initial_ledger(nodes)
    first = choose_placement(
        service=service,
        replica_id="r1",
        node_specs=nodes,
        node_ledger=ledger,
    )
    assert first.placement is not None
    assert first.placement.node == "n1"
    assert ledger["n1"]["used_instances"] == 1

    duplicate = choose_placement(
        service=service,
        replica_id="r1",
        node_specs=nodes,
        node_ledger=ledger,
        existing=first.to_dict()["placement"],
    )
    assert duplicate.idempotent is True
    assert ledger["n1"]["used_instances"] == 1

    second = choose_placement(
        service=service,
        replica_id="r2",
        node_specs=nodes,
        node_ledger=ledger,
    )
    assert second.placement is not None
    assert second.placement.gpu_index == 1
    assert ledger["n1"]["used_instances"] == 2

    rejected = choose_placement(
        service=service,
        replica_id="r3",
        node_specs=nodes,
        node_ledger=ledger,
    )
    assert rejected.placement is None
    assert rejected.first_constraint == "NODE_CONCURRENCY_EXHAUSTED"
    assert ledger["n1"]["used_instances"] == 2
    assert rejected.evidence["partial_placement"] is False


def test_packing_rejects_topology_and_memory_without_partial_claim(base_spec) -> None:
    service = base_spec.services["embed-beta"]
    nodes = {
        "wrong-zone": NodeSpec("wrong-zone", 4, 8192, 8, "zone-a"),
        "small-gpu": NodeSpec("small-gpu", 1, 2048, 8, "zone-b"),
    }
    ledger = make_initial_ledger(nodes)
    decision = choose_placement(
        service=service,
        replica_id="r1",
        node_specs=nodes,
        node_ledger=ledger,
    )
    assert decision.placement is None
    assert decision.first_constraint in {
        "TOPOLOGY_DOMAIN_MISMATCH",
        "GPU_MEMORY_EXHAUSTED",
    }
    reasons = {item["node"]: item["reason"] for item in decision.eliminated}
    assert reasons == {
        "small-gpu": "GPU_MEMORY_EXHAUSTED",
        "wrong-zone": "TOPOLOGY_DOMAIN_MISMATCH",
    }
    assert ledger == make_initial_ledger(nodes)
