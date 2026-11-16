from __future__ import annotations

import pytest

from lineage_migrator.errors import BusinessCode, MigrationError
from lineage_migrator.engine import Engine
from lineage_migrator.models import Rule


def test_baseline_and_versions_persisted(replayed_store):
    assert replayed_store.last_version() == 5
    versions = [row["version"] for row in replayed_store.versions()]
    assert versions == [0, 1, 2, 3, 4, 5]
    assert len(replayed_store.snapshot_edges(0)) == 9


def test_rename_rewrites_edge_endpoints(fresh_engine, sample):
    engine = fresh_engine()
    engine.apply_version(1, sample.versions[1])
    edges = {edge.edge_id: edge for edge in engine.store.snapshot_edges(1)}
    assert edges["e0002"].target_ref == "dwd_order.user_id"
    assert edges["e0005"].target_ref == "dwd_order.user_id"
    assert "dwd_order.user_id" in {item.ref for item in engine.store.live_fields()}


def test_type_change_keeps_edges_intact(fresh_engine, sample):
    engine = fresh_engine()
    before = {edge.edge_id for edge in engine.store.snapshot_edges(0)}
    engine.apply_version(1, sample.versions[1])
    engine.apply_version(2, sample.versions[2])
    after = {edge.edge_id for edge in engine.store.snapshot_edges(2)}
    assert before == after
    field = next(item for item in engine.store.snapshot_fields(2) if item.ref == "orders.amount")
    assert field.data_type == "DECIMAL(18,4)"


def test_split_distributes_edges_and_removes_source(fresh_engine, sample):
    engine = fresh_engine()
    engine.apply_version(1, sample.versions[1])
    engine.apply_version(2, sample.versions[2])
    engine.apply_version(3, sample.versions[3])
    refs = {item.ref for item in engine.store.snapshot_fields(3)}
    assert "dwd_order.amount_usd" not in refs
    assert "ads_report.gmv_usd_split" in refs
    assert "ads_report.gmv_cny_split" in refs
    origins = [edge.origin for edge in engine.store.snapshot_edges(3)]
    assert any(origin.startswith("split:r3_split_amount") for origin in origins)


def test_merge_keeps_provenance_in_origin(fresh_engine, sample):
    engine = fresh_engine()
    for version in (1, 2, 3, 4):
        engine.apply_version(version, sample.versions[version])
    refs = {item.ref for item in engine.store.snapshot_fields(4)}
    assert "customers.full_name" in refs
    assert "customers.first_name" not in refs
    edges = engine.store.snapshot_edges(4)
    assert any(edge.origin.startswith("merge:r4_merge_customer_name") for edge in edges)


def test_deprecate_deletes_incident_edges_and_audits(fresh_engine, sample):
    engine = fresh_engine()
    for version in (1, 2, 3, 4, 5):
        engine.apply_version(version, sample.versions[version])
    edges = engine.store.snapshot_edges(5)
    assert all(edge.source_ref != "orders.amount_cent" for edge in edges)
    field = next(item for item in engine.store.snapshot_fields(5) if item.ref == "orders.amount_cent")
    assert field.deprecated is True
    events = engine.store.audit_events(5)
    assert events[0]["event_type"] == "deprecated_edges_removed"
    assert events[0]["payload"]["deleted_edges"]


def test_rule_order_conflict_rolls_back(fresh_engine):
    engine = fresh_engine()
    rules = [
        Rule(rule_id="a", rule_type="type_change", source_ref="orders.amount", new_type="STRING"),
        Rule(rule_id="b", rule_type="rename", source_ref="orders.amount", target_ref="orders.amount2"),
    ]
    with pytest.raises(MigrationError) as exc:
        engine.apply_version(1, rules)
    assert exc.value.code == BusinessCode.E_RULE_ORDER
    assert engine.store.last_version() == 0


def test_explicit_seq_resolves_order(fresh_engine):
    engine = fresh_engine()
    rules = [
        Rule(rule_id="b", rule_type="rename", source_ref="orders.amount",
             target_ref="orders.amount2", seq=2),
        Rule(rule_id="a", rule_type="type_change", source_ref="orders.amount",
             new_type="STRING", seq=1),
    ]
    result = engine.apply_version(1, rules)
    assert result["status"] == "committed"
    refs = {item.ref for item in engine.store.live_fields()}
    assert "orders.amount2" in refs


def test_depends_on_topological_order(fresh_engine):
    engine = fresh_engine()
    rules = [
        Rule(rule_id="b", rule_type="rename", source_ref="orders.amount",
             target_ref="orders.amount2", depends_on=("a",)),
        Rule(rule_id="a", rule_type="type_change", source_ref="orders.amount", new_type="STRING"),
    ]
    engine.apply_version(1, rules)
    assert "orders.amount2" in {item.ref for item in engine.store.live_fields()}


def test_duplicate_seq_rejected(fresh_engine):
    engine = fresh_engine()
    rules = [
        Rule(rule_id="a", rule_type="type_change", source_ref="orders.amount", new_type="STRING", seq=1),
        Rule(rule_id="b", rule_type="type_change", source_ref="orders.order_id", new_type="STRING", seq=1),
    ]
    with pytest.raises(MigrationError) as exc:
        engine.apply_version(1, rules)
    assert exc.value.code == BusinessCode.E_RULE_ORDER


def test_version_regression_rejected(replayed_store):
    engine = Engine(replayed_store)
    rule = Rule(rule_id="r", rule_type="type_change", source_ref="orders.amount", new_type="STRING")
    with pytest.raises(MigrationError) as exc:
        engine.apply_version(3, [rule])
    assert exc.value.code == BusinessCode.E_VERSION_REGRESS


def test_stale_source_after_deprecation(replayed_store):
    engine = Engine(replayed_store)
    rule = Rule(rule_id="r", rule_type="type_change",
                source_ref="orders.amount_cent", new_type="STRING")
    with pytest.raises(MigrationError) as exc:
        engine.apply_version(6, [rule])
    assert exc.value.code == BusinessCode.E_STALE_SOURCE
    assert replayed_store.last_version() == 5


def test_target_missing(replayed_store):
    engine = Engine(replayed_store)
    rule = Rule(rule_id="r", rule_type="rename",
                source_ref="orders.ghost", target_ref="orders.ghost2")
    with pytest.raises(MigrationError) as exc:
        engine.apply_version(6, [rule])
    assert exc.value.code == BusinessCode.E_TARGET_MISSING


def test_cycle_merge_self_loop(replayed_store):
    engine = Engine(replayed_store)
    rule = Rule(
        rule_id="r",
        rule_type="merge",
        sources=("orders.order_id", "dwd_order.order_id"),
        target_ref="dwd_order.order_id",
    )
    with pytest.raises(MigrationError) as exc:
        engine.apply_version(6, [rule])
    assert exc.value.code == BusinessCode.E_CYCLE
    assert replayed_store.last_version() == 5
