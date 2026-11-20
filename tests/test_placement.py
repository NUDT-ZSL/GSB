"""Requirement 3: replica placement under VRAM + concurrency budgets with
topology constraints, all-or-nothing semantics and rejection records."""
from gateway import errors
from gateway.models import NodeSpec, ServiceSpec
from gateway.placement import (fresh_node_states, place_service,
                               plan_replica_count)


def _service(**kw):
    base = dict(name="svc", model_version="v1", input_tokens=10,
                output_tokens=10, compute_units=10, vram_mib=1024,
                min_replicas=1, max_replicas=4, batch_window_ms=10,
                cold_start_ms=0, topology_domain="any")
    base.update(kw)
    return ServiceSpec(**base)


def _nodes():
    return {
        "n1": NodeSpec("n1", gpu_cards=1, vram_per_card_mib=4096,
                       max_instances=2, topology_domain="d1"),
        "n2": NodeSpec("n2", gpu_cards=1, vram_per_card_mib=2048,
                       max_instances=1, topology_domain="d2"),
    }


def test_vram_budget_first_fit_and_remaining():
    svc = _service(vram_mib=2048)
    states = fresh_node_states(_nodes())
    result = place_service(svc, 2, states, set())
    assert result["status"] == "placed"
    # both replicas land on n1 (first-fit), card 0 keeps 0 MiB
    assert [r["node"] for r in result["replicas"]] == ["n1", "n1"]
    assert states["n1"].cards[0] == 0
    assert states["n1"].instances == 2


def test_concurrency_cap_triggers():
    svc = _service(vram_mib=512)
    states = fresh_node_states(_nodes())
    # n1 allows 2 instances, n2 allows 1 -> 4 replicas cannot be placed
    result = place_service(svc, 4, states, set())
    assert result["status"] == "failed"
    assert result["first_constraint"] in (
        errors.E_PLACEMENT_CONCURRENCY, errors.E_PLACEMENT_VRAM)
    reasons = {c["reason"] for c in result["rejected_candidates"]}
    assert errors.E_PLACEMENT_CONCURRENCY in reasons


def test_all_or_nothing_no_partial_consumption():
    svc = _service(vram_mib=2048)
    states = fresh_node_states(_nodes())
    result = place_service(svc, 3, states, set())  # n1 fits 2, n2 fits 1
    assert result["status"] == "placed"
    result = place_service(svc, 4, fresh_node_states(_nodes()), set())
    assert result["status"] == "failed"
    # failed placement consumed nothing anywhere
    states = fresh_node_states(_nodes())
    place_service(svc, 4, states, set())
    assert states["n1"].cards[0] == 4096
    assert states["n1"].instances == 0
    assert states["n2"].cards[0] == 2048


def test_topology_domain_constraint():
    svc = _service(topology_domain="d2", vram_mib=1024)
    states = fresh_node_states(_nodes())
    result = place_service(svc, 1, states, set())
    assert result["status"] == "placed"
    assert result["replicas"][0]["node"] == "n2"
    reasons = {c["reason"] for c in result["rejected_candidates"]}
    assert errors.E_PLACEMENT_DOMAIN in reasons


def test_rejected_candidates_record_first_constraint():
    svc = _service(vram_mib=8192)  # fits nowhere
    result = place_service(svc, 1, fresh_node_states(_nodes()), set())
    assert result["status"] == "failed"
    assert result["first_constraint"] == errors.E_PLACEMENT_VRAM
    assert result["rejected_candidates"]


def test_replica_count_clamped_to_bounds():
    svc = _service(min_replicas=2, max_replicas=3, compute_units=10)
    assert plan_replica_count(svc, 0, 1000) == 2
    assert plan_replica_count(svc, 10 ** 9, 1000) == 3
    assert plan_replica_count(svc, 25000, 1000) == 3
    assert plan_replica_count(svc, 15000, 1000) == 2


def test_engine_records_card_remaining_and_route_targets(loaded, tmp_path):
    from gateway.engine import Engine
    from gateway.store import Store
    services, nodes, trace = loaded
    store = Store(str(tmp_path / "r.db"))
    try:
        report = Engine(services, nodes, trace, store).run()
    finally:
        store.close()
    for rnd in report["rounds"]:
        placed = {(p["service"], r["node"]) for p in rnd["placements"]
                  for r in p["replicas"]}
        assert placed  # every window places at least one replica
        for snap in rnd["node_states"]:
            node = nodes[snap["node"]]
            for remaining in snap["cards_remaining_mib"]:
                assert 0 <= remaining <= node.vram_per_card_mib
            assert snap["instances_used"] <= node.max_instances
