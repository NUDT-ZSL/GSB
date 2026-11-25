from __future__ import annotations

import numpy as np
import pytest

from moe_router.cache import ContentAddressedCache
from moe_router.codec import canonical_bytes
from moe_router.errors import E_BLOCK_MISMATCH, E_BLOCK_NOT_READY, E_RESOURCE_LIMIT, MoeError
from moe_router.models import ResourceLimits
from moe_router.routing import (
    RoutingSession,
    compare_routes,
    compute_full_reference,
    gate_tokens,
)
from moe_router.weights import build_weight_bundle, generate_cases


def test_gate_artifacts_are_byte_deterministic() -> None:
    case = generate_cases(count=1)[0]
    bundle = build_weight_bundle(case)
    first = gate_tokens(bundle.tokens, bundle.gate_weights, case.top_k)
    second = gate_tokens(bundle.tokens, bundle.gate_weights, case.top_k)
    assert canonical_bytes(
        {
            "raw": first.raw_scores,
            "prob": first.probabilities,
            "selected": first.selected_experts,
            "gate": first.selected_gate,
        }
    ) == canonical_bytes(
        {
            "raw": second.raw_scores,
            "prob": second.probabilities,
            "selected": second.selected_experts,
            "gate": second.selected_gate,
        }
    )


def test_block_query_readiness_and_metadata(tmp_path) -> None:
    case = generate_cases(count=1)[0]
    bundle = build_weight_bundle(case)
    session = RoutingSession(bundle, ContentAddressedCache(tmp_path / "cache"))
    session.compute_next_block()
    block = session.query_block(0)
    assert block["ready"] is True
    assert block["token_start"] == 0
    assert block["token_end"] == case.block_size
    assert block["expert_revisions_used"]
    assert block["capacity_summary"]["capacity_per_expert"] >= 1
    assert set(block["cache_records"]) == {
        "gate_distribution",
        "expert_assignment",
        "block_output",
        "block_metadata",
    }
    assert block["first_block_query_ms"] is not None
    with pytest.raises(MoeError) as exc_info:
        session.query_block(1)
    assert exc_info.value.code == E_BLOCK_NOT_READY
    assert exc_info.value.block_index == 1


def test_block_and_full_reference_are_token_exact(tmp_path) -> None:
    for case in generate_cases(count=3):
        bundle = build_weight_bundle(case)
        session = RoutingSession(bundle, ContentAddressedCache(tmp_path / case.case_id))
        session.compute_next_block()
        session.query_block(0)
        session.route_all()
        report = session.consistency_report(raise_on_mismatch=True)
        assert report["consistent"] is True
        assert report["max_abs_diff_gate"] == 0.0
        assert report["max_abs_diff_expert"] == 0.0
        assert report["max_abs_diff_output"] == 0.0
        assert report["first_diff_token"] is None
        assert all(
            block.assignment.capacity_summary["capacity_per_expert"]
            == block.assignment.capacity_per_expert
            for block in session.blocks.values()
        )


def test_capacity_is_block_local(tmp_path) -> None:
    case = generate_cases(count=1)[0]
    bundle = build_weight_bundle(case)
    session = RoutingSession(bundle, ContentAddressedCache(tmp_path / "cache")).route_all()
    capacities = [
        session.blocks[index].assignment.capacity_per_expert
        for index in range(session.block_count)
    ]
    assert capacities == [2, 2, 2]
    for block in session.blocks.values():
        assert sum(block.assignment.accepted_per_expert) <= (
            block.assignment.capacity_per_expert * case.num_experts
        )
        assert all(
            count <= block.assignment.capacity_per_expert
            for count in block.assignment.accepted_per_expert
        )
        assert len(block.assignment.overflow) == (
            block.assignment.capacity_summary["total_overflow"]
        )


def test_mismatch_reports_first_token_and_expert_detail(tmp_path) -> None:
    case = generate_cases(count=1)[0]
    bundle = build_weight_bundle(case)
    session = RoutingSession(bundle, ContentAddressedCache(tmp_path / "cache")).route_all()
    reference = compute_full_reference(bundle)
    perturbed = session.assembled_outputs().copy()
    perturbed[2, 0] += 1e-3
    with pytest.raises(MoeError) as exc_info:
        compare_routes(
            case,
            actual_outputs=perturbed,
            actual_gate=session.assembled_gate(),
            actual_assignments=session.assembled_assignments(),
            actual_contributions=session.assembled_contributions(),
            reference=reference,
            tolerance=1e-12,
            raise_on_mismatch=True,
        )
    error = exc_info.value
    assert error.code == E_BLOCK_MISMATCH
    assert error.details["first_diff_token"] == 2
    detail = error.details["first_diff_details"]
    assert detail["token_index"] == 2
    assert len(detail["expert_contributions"]) == case.top_k
    assert len(detail["actual_gate_distribution"]) == case.num_experts

    report = compare_routes(
        case,
        actual_outputs=perturbed,
        actual_gate=session.assembled_gate(),
        actual_assignments=session.assembled_assignments(),
        actual_contributions=session.assembled_contributions(),
        reference=reference,
        tolerance=1e-12,
        raise_on_mismatch=False,
    )
    assert report["consistent"] is False


def test_resident_memory_limit(tmp_path) -> None:
    case = generate_cases(count=1)[0]
    bundle = build_weight_bundle(case)
    session = RoutingSession(
        bundle,
        ContentAddressedCache(tmp_path / "cache"),
        limits=ResourceLimits(max_case_resident_bytes=1),
    )
    with pytest.raises(MoeError) as exc_info:
        session.compute_next_block()
    assert exc_info.value.code == E_RESOURCE_LIMIT
    assert exc_info.value.field == "max_case_resident_bytes"


def test_repeated_runs_produce_identical_cache_content(tmp_path) -> None:
    case = generate_cases(count=1)[0]
    bundle = build_weight_bundle(case)
    hashes = []
    for name in ("first", "second"):
        cache = ContentAddressedCache(tmp_path / name)
        session = RoutingSession(bundle, cache).route_all()
        hashes.append(
            [
                session.blocks[index].cache_records[kind]["content_hash"]
                for index in range(session.block_count)
                for kind in sorted(session.blocks[index].cache_records)
            ]
        )
        assert cache.verify_all_objects()["ok"] is True
    assert hashes[0] == hashes[1]
