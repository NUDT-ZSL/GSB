from __future__ import annotations

import json
from pathlib import Path

from lineage_migrator.diff import diff_versions
from lineage_migrator.engine import Engine
from lineage_migrator.errors import BusinessCode, MigrationError
from lineage_migrator.models import Rule
from lineage_migrator.store import Store


def test_rollback_boundary_restores_previous_snapshot(replayed_store, tmp_path):
    fingerprint_v4 = replayed_store.get_version(4)["fingerprint"]
    rule = Rule(rule_id="bad", rule_type="rename",
                source_ref="orders.ghost", target_ref="orders.ghost2")
    engine = Engine(replayed_store)
    try:
        engine.apply_version(6, [rule])
    except MigrationError as exc:
        assert exc.code == BusinessCode.E_TARGET_MISSING
    assert replayed_store.last_version() == 5

    replayed_store.rollback_to(4)
    assert replayed_store.last_version() == 4
    assert replayed_store.get_version(4)["fingerprint"] == fingerprint_v4
    assert replayed_store.get_version(5) is None


def test_replay_after_rollback_is_independent(replayed_store, sample):
    store = replayed_store
    store.rollback_to(3)
    engine = Engine(store)
    engine.apply_version(4, sample.versions[4])
    engine.apply_version(5, sample.versions[5])
    assert store.last_version() == 5
    edges_now = {edge.edge_id for edge in store.snapshot_edges(5)}

    fresh = Engine.bootstrap(Path(store.db_path).parent / "independent.db", sample)
    fresh.replay_all(sample)
    edges_fresh = {edge.edge_id for edge in fresh.store.snapshot_edges(5)}
    assert edges_now == edges_fresh


def test_replay_bit_identical(tmp_path, sample):
    outputs = []
    for index in (1, 2):
        engine = Engine.bootstrap(tmp_path / f"r{index}.db", sample)
        engine.replay_all(sample)
        outputs.append(engine.store.versions())
    assert outputs[0] == outputs[1]


def test_diff_report_matches_snapshots_only(replayed_store):
    report = diff_versions(replayed_store, 0, replayed_store.last_version())
    snapshot_ids = {edge.edge_id for edge in replayed_store.snapshot_edges(replayed_store.last_version())}
    for item in report["added"]:
        assert item["edge_id"] in snapshot_ids
    assert report["summary"]["added"] >= 1
    assert report["summary"]["removed"] >= 1
    assert report["summary"]["endpoint_changed"] >= 1
    for item in report["added"] + report["removed"] + report["endpoint_changed"]:
        assert item["attribution"]


def test_diff_table_drilldown(replayed_store):
    report = diff_versions(replayed_store, 0, 5, table="orders")
    for item in report["added"]:
        assert "orders." in item["source_ref"] or "orders." in item["target_ref"]


def test_diff_unknown_version_raises(replayed_store):
    import pytest

    with pytest.raises(ValueError):
        diff_versions(replayed_store, 0, 99)


def test_crash_recovery_reads_last_committed_version(tmp_path, sample):
    db_path = tmp_path / "crash.db"
    engine = Engine.bootstrap(db_path, sample)
    engine.apply_version(1, sample.versions[1])
    engine.apply_version(2, sample.versions[2])

    reopened = Store(db_path)
    assert reopened.last_version() == 2
    assert reopened.get_version(2)["status"] == "committed"
    reopened.close()


def test_failed_attempt_leaves_no_trace(replayed_store):
    engine = Engine(replayed_store)
    rule = Rule(rule_id="ghost", rule_type="rename",
                source_ref="orders.ghost", target_ref="orders.ghost2")
    try:
        engine.apply_version(6, [rule])
    except MigrationError:
        pass
    assert replayed_store.get_version(6) is None
    assert all(row["version"] != 6 for row in replayed_store.change_log())
