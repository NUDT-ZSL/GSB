"""Requirements 2, 3, 5: merge, migration semantics, clock determinism."""

from __future__ import annotations

import pytest

from conftest import CLOCK_BASE, build_plan, make_clock
from recovery_pipeline.clock import LogicalClock
from recovery_pipeline.errors import BizError, Err
from recovery_pipeline.merge import merge_configs
from recovery_pipeline.migrate import (migration_invariant_holds,
                                       should_rollback)


def test_merge_winner_and_provenance(samples):
    plan, _, _ = build_plan(samples)
    assert set(plan.services) == {"svc-alpha", "svc-beta", "svc-gamma",
                                  "svc-delta"}
    assert "svc-legacy" not in plan.services  # tombstoned

    beta = plan.services["svc-beta"]
    assert beta.actions[0].source_version == "v2"  # higher generation wins
    assert beta.actions[0].original_time_repr == "ms:900000"
    assert beta.actions[0].original_rollback_seconds is None
    assert beta.rollback_abs_ms == 900000  # v2 absolute kept as-is

    alpha = plan.services["svc-alpha"]
    assert all(a.source_version == "v1" for a in alpha.actions)
    assert alpha.actions[0].original_time_repr == "tick:0"
    assert alpha.actions[0].original_rollback_seconds == 30
    assert alpha.rollback_abs_ms == CLOCK_BASE + 30_000


def test_merge_stable_action_order(samples):
    plan, _, _ = build_plan(samples)
    alpha = plan.services["svc-alpha"]
    ticks = [a.effective_ms for a in alpha.actions]
    assert ticks == sorted(ticks)
    ids = [a.action_id for a in alpha.actions]
    assert ids == ["alpha-stop", "alpha-switch", "alpha-start"]


def test_merge_format_priority_tiebreak(samples):
    # Equal generation and declared clock -> v2 format priority wins.
    _, v1, v2 = None, None, None
    from recovery_pipeline.models import (V1Action, V1Config, V1Service,
                                          V2Config, V2Service)
    v1 = V1Config(services=(V1Service(
        name="svc-x", fault_domain="fd", rollback_seconds=10,
        actions=(V1Action("x1", 5, "switch"),)),), action_map={"svc-x": ["x1"]})
    v2 = V2Config(manifest_version=1, generation_floor=1, services=(V2Service(
        name="svc-x", effective_ms=5, generation=1, priority="P1",
        upstream=(), tombstone=False),), shard_digests={})
    plan = merge_configs(v1, v2, CLOCK_BASE)
    assert plan.services["svc-x"].actions[0].source_version == "v2"


def test_migration_map_and_invariant(samples):
    plan, _, _ = build_plan(samples)
    record = plan.migration_map["svc-alpha"]
    assert record["scale"] == "1s=1000ms"
    assert record["rounding"] == "EXACT"
    assert record["generation_default"] == 1
    assert record["priority_default"] == "P3"
    for now in (CLOCK_BASE - 1, CLOCK_BASE, CLOCK_BASE + 29_999,
                CLOCK_BASE + 30_000, CLOCK_BASE + 10**9):
        assert migration_invariant_holds(plan, "svc-alpha", now)
    # v2-only services have no migration record and are trivially invariant
    assert migration_invariant_holds(plan, "svc-gamma", CLOCK_BASE)
    assert should_rollback(900000, 900000)
    assert not should_rollback(900000, 899999)


def test_logical_clock_deterministic_replay():
    seq = [100, 200, 300, 400]
    clock_a, clock_b = LogicalClock(seq), LogicalClock(seq)
    first = [clock_a.now_ms() for _ in seq]
    second = [clock_b.now_ms() for _ in seq]
    assert first == second == seq


def test_logical_clock_rollback_beyond_tolerance():
    clock = LogicalClock([10_000, 9_000], rollback_tolerance_ms=500)
    assert clock.now_ms() == 10_000
    with pytest.raises(BizError) as ei:
        clock.now_ms()
    assert ei.value.code == Err.CLOCK_ROLLBACK_EXCEEDED
    assert ei.value.fields["rollback_ms"] == 1000


def test_logical_clock_rollback_within_tolerance():
    clock = LogicalClock([10_000, 9_800], rollback_tolerance_ms=500)
    assert clock.now_ms() == 10_000
    assert clock.now_ms() == 9_800
