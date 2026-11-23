from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from conftest import make_engine


def test_equal_candidate_scores_are_marked_precision_sensitive(tmp_path: Path) -> None:
    layers = [
        {"digest": "sha256:l1", "bytes": 1000, "node": "node-a", "ref_count": 1},
        {"digest": "sha256:l2", "bytes": 1000, "node": "node-a", "ref_count": 1},
    ]
    instances = [
        {
            "instance_id": "a",
            "service": "svc",
            "priority": 10,
            "requested_ephemeral_bytes": 1000,
            "required_layers": ["sha256:l1"],
            "deadline_ms": 100,
        },
        {
            "instance_id": "b",
            "service": "svc",
            "priority": 10,
            "requested_ephemeral_bytes": 1000,
            "required_layers": ["sha256:l2"],
            "deadline_ms": 100,
        },
    ]
    nodes = [
        {
            "node": "node-a",
            "available_ephemeral_bytes": 100_000,
            "image_layer_bytes": 2000,
            "max_concurrent_cold_starts": 1,
        }
    ]
    engine = make_engine(
        tmp_path,
        instances=instances,
        layers=layers,
        nodes=nodes,
        config={"comparison_tolerance_ticks": 2, "error_upper_bound_budget": 8},
    )
    result = engine.run_round("r1", 1).to_dict()
    state = engine.snapshot()["state"]
    assert result["summary"]["precision_sensitive"] is True
    assert len(state["precision_flags"]) == 1
    flag = state["precision_flags"][0]
    assert flag["context"] == "admission_order"
    assert {flag["first_instance_id"], flag["second_instance_id"]} == {"a", "b"}
    assert flag["fixed_score_diff"] <= 2
    assert flag["sources_first"]
    assert flag["sources_second"]
    for instance_id, charged in (("a", True), ("b", False)):
        error = state["error_ledger"][instance_id]
        assert Decimal(error["read_abs_deviation"]) <= Decimal(8)
        assert error["decimal_read_ticks"] != ""
        assert error["sources"]
        assert error["quote_entries"]
        assert error["charged"] is charged
        assert error["within_error_budget"] is True

