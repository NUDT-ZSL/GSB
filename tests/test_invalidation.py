from __future__ import annotations

import numpy as np

from moe_router.cache import ContentAddressedCache
from moe_router.invalidation import InvalidationEngine
from moe_router.models import ResourceLimits
from moe_router.routing import RoutingSession
from moe_router.weights import (
    build_expert_bank,
    build_weight_bundle,
    generate_cases,
)


def _case_with_reusable_prefix() -> tuple[object, int]:
    for case in generate_cases(count=6):
        bundle = build_weight_bundle(case)
        # A lightweight gate-only analysis is enough to find a late first use.
        from moe_router.routing import gate_tokens

        gates = gate_tokens(bundle.tokens, bundle.gate_weights, case.top_k)
        for expert_id in range(case.num_experts):
            selected = [
                token // case.block_size
                for token in range(case.token_len)
                if expert_id in set(gates.selected_experts[token].tolist())
            ]
            if selected and min(selected) > 0:
                return case, expert_id
    raise AssertionError("fixture has no expert with a reusable prefix")


def _revised_bundle(case, old_bank, changed_expert: int):
    new_revision = case.router_revision + 1
    new_case = case.with_updates(router_revision=new_revision)
    revisions = {
        expert_id: (
            new_revision if expert_id == changed_expert else old_bank.revisions[expert_id]
        )
        for expert_id in range(case.num_experts)
    }
    bundle = build_weight_bundle(new_case, revisions)
    assert bundle.expert_bank.digest == build_expert_bank(new_case, revisions).digest
    return new_case, bundle


def test_suffix_invalidation_reuses_prefix_and_is_consistent(tmp_path) -> None:
    case, changed_expert = _case_with_reusable_prefix()
    old_bundle = build_weight_bundle(case)
    cache = ContentAddressedCache(tmp_path / "cache")
    old_session = RoutingSession(old_bundle, cache).route_all()
    new_case, new_bundle = _revised_bundle(
        case, old_bundle.expert_bank, changed_expert
    )
    engine = InvalidationEngine(
        ResourceLimits(max_parallel_recompute_blocks=2)
    )
    result = engine.apply(
        old_session=old_session,
        new_bundle=new_bundle,
        strategy="suffix",
        parallel_blocks=2,
        check_consistency=True,
    )
    plan = result.plan
    assert changed_expert in plan["changed_experts"]
    assert plan["recompute_suffix_from"] == min(plan["exact_affected_token_blocks"])
    assert plan["reused_blocks"]
    assert plan["recompute_blocks"]
    assert plan["reused_blocks"][0] == 0
    assert plan["serialized_due_to_limit"] is False
    assert result.consistency_report["consistent"] is True
    assert result.consistency_report["max_abs_diff_output"] == 0.0
    for index in plan["reused_blocks"]:
        assert result.session.blocks[index].reused is True
        assert (
            result.session.blocks[index].outputs
            == old_session.blocks[index].outputs
        ).all()
        assert result.session.blocks[index].reused is True
        assert all(
            record["reused_from"] is not None
            for record in result.session.blocks[index].cache_records.values()
        )
    for index in plan["recompute_blocks"]:
        assert result.session.blocks[index].reused is False

    fresh = RoutingSession(new_bundle, cache).route_all()
    assert fresh.consistency_report()["consistent"] is True


def test_targeted_strategy_reuses_non_adjacent_unaffected_blocks(tmp_path) -> None:
    case, changed_expert = _case_with_reusable_prefix()
    old_bundle = build_weight_bundle(case)
    cache = ContentAddressedCache(tmp_path / "cache")
    old_session = RoutingSession(old_bundle, cache).route_all()
    _new_case, new_bundle = _revised_bundle(
        case, old_bundle.expert_bank, changed_expert
    )
    result = InvalidationEngine().apply(
        old_session=old_session,
        new_bundle=new_bundle,
        strategy="targeted",
        check_consistency=True,
    )
    assert result.plan["recompute_blocks"] == result.plan[
        "exact_affected_token_blocks"
    ]
    assert result.plan["reused_blocks"]
    assert result.consistency_report["consistent"] is True


def test_over_parallel_limit_falls_back_to_declared_serial_order(tmp_path) -> None:
    case, changed_expert = _case_with_reusable_prefix()
    old_bundle = build_weight_bundle(case)
    cache = ContentAddressedCache(tmp_path / "cache")
    old_session = RoutingSession(old_bundle, cache).route_all()
    _new_case, new_bundle = _revised_bundle(
        case, old_bundle.expert_bank, changed_expert
    )
    result = InvalidationEngine(
        ResourceLimits(max_parallel_recompute_blocks=1)
    ).apply(
        old_session=old_session,
        new_bundle=new_bundle,
        strategy="suffix",
        parallel_blocks=4,
        check_consistency=True,
    )
    assert result.plan["requested_parallel_blocks"] == 4
    assert result.plan["effective_parallel_blocks"] == 1
    assert result.plan["serialized_due_to_limit"] is True
    assert result.plan["recompute_blocks"] == sorted(result.plan["recompute_blocks"])
    assert result.consistency_report["consistent"] is True


def test_policy_change_invalidates_every_block(tmp_path) -> None:
    case = generate_cases(count=1)[0]
    old_bundle = build_weight_bundle(case)
    cache = ContentAddressedCache(tmp_path / "cache")
    old_session = RoutingSession(old_bundle, cache).route_all()
    new_case = case.with_updates(
        router_revision=case.router_revision + 1,
        top_k=case.top_k + 1,
    )
    revisions = {
        expert_id: revision
        for expert_id, revision in enumerate(old_bundle.expert_bank.revisions)
    }
    new_bundle = build_weight_bundle(new_case, revisions)
    result = InvalidationEngine().apply(
        old_session=old_session,
        new_bundle=new_bundle,
        strategy="suffix",
        check_consistency=True,
    )
    assert result.plan["policy_changed"] is True
    assert result.plan["reused_blocks"] == []
    assert result.plan["recompute_blocks"] == list(range(new_case.block_count))
    assert result.consistency_report["consistent"] is True
