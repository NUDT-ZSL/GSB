from __future__ import annotations

from deploy_convergence.canonical import canonical_json
from deploy_convergence.migration import migrate_v1_entries


def test_merge_provenance_conflicts_tombstones_and_degradation(
    engine_factory,
    clock_ms: int,
) -> None:
    result = engine_factory().run(clock_ms=clock_ms)
    view = result.view
    assert view is not None
    dev = view["environments"]["development"]

    orders = dev["orders-api"]
    assert orders["params"]["timeout_ms"]["value"] == 850
    assert orders["params"]["timeout_ms"]["source_version"] == "v2"
    assert (
        orders["params"]["timeout_ms"]["original_time"]["kind"] == "absolute_ms"
    )
    order_conflict = [
        item
        for item in view["conflicts"]
        if item["service"] == "orders-api"
        and item["parameter"] == "timeout_ms"
        and item["environment"] == "development"
    ]
    assert len(order_conflict) == 1
    assert order_conflict[0]["selected"]["value"] == 850
    assert order_conflict[0]["discarded"][0]["value"] == 800
    assert (
        order_conflict[0]["decision_basis"] == "higher_effective_at_ms"
    )

    assert (
        dev["auth-service"]["decision"]["basis"]
        == "higher_source_priority"
    )
    assert dev["auth-service"]["params"]["timeout_ms"]["source_version"] == "v2"

    reporting = dev["reporting-api"]
    assert reporting["selected_source"]["source_version"] == "v1"
    assert reporting["params"]["strategy"]["value"] == "rolling"
    assert reporting["params"]["strategy"]["compatibility_default"] is True
    degradation = [
        item
        for item in view["compatibility_degradations"]
        if item["environment"] == "development"
        and item["service"] == "reporting-api"
        and item["parameter"] == "strategy"
    ]
    assert degradation[0]["reason"] == "v2_field_absent_in_v1"
    assert degradation[0]["mapping"] == "declared_default"

    legacy = dev["legacy-worker"]
    assert legacy["tombstoned"] is True
    assert legacy["params"] == {}
    assert any(
        item["service"] == "legacy-worker"
        and item["kind"] == "tombstone_vs_live"
        for item in view["conflicts"]
    )
    assert dev["retired-cache"]["expired_at_clock"] is True


def test_v1_migration_is_idempotent_and_preserves_time_semantics(
    engine_factory,
    clock_ms: int,
) -> None:
    engine = engine_factory()
    parsed = engine.loader.load(
        clock_ms=clock_ms,
        injections=engine.injections,
    ).payload
    first = migrate_v1_entries(parsed, clock_ms=clock_ms)
    second = migrate_v1_entries(first, clock_ms=clock_ms)
    assert canonical_json(first) == canonical_json(second)

    migration = next(
        item
        for item in first["migrations"]
        if item["environment"] == "development"
        and item["service"] == "auth-service"
    )
    assert migration["time"]["from"]["scale_ms"] == 1000
    assert migration["time"]["from"]["rounding"] == "floor"
    assert migration["time"]["to"]["value"] == 1_735_689_600_125
    assert migration["generation"]["to"] == 0
    assert migration["upstreams"]["to"] == []
    assert migration["expiry"]["preserved"] is True

    defaults = next(
        item
        for item in first["compatibility_degradations"]
        if item["environment"] == "development"
        and item["service"] == "reporting-api"
        and item["parameter"] == "max_burst"
    )
    assert defaults["default_value"] == 0
