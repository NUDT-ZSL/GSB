from __future__ import annotations

import json
from pathlib import Path

from sensitivity_engine.atomicio import atomic_write_json
from sensitivity_engine.checkpointing import (
    recover_to_checkpoint,
    verify_checkpoint,
    write_checkpoint,
)
from sensitivity_engine.hashing import canonical_json
from sensitivity_engine.loader import load_dataset
from sensitivity_engine.service import import_dataset
from sensitivity_engine.store import Store


def test_checkpoint_verifies_and_recovers_corrupt_ledger(dataset, tmp_path):
    db = tmp_path / "ledger.sqlite3"
    snapshot = tmp_path / "checkpoint.json"
    store = Store(db)
    import_dataset(store, dataset)
    write_checkpoint(store, snapshot)
    assert verify_checkpoint(store, snapshot)["ok"] is True

    store.conn.execute(
        "UPDATE object_ledger SET effective_level='PUBLIC' WHERE object_key='obj:empty'"
    )
    failed = verify_checkpoint(store, snapshot)
    assert failed["ok"] is False
    assert any(issue["code"] == "E_LEDGER_MISMATCH" for issue in failed["issues"])

    stale = snapshot.parent / ".checkpoint.json.crash.tmp"
    stale.write_text("{partial", encoding="utf-8")

    recovery = recover_to_checkpoint(store, snapshot, dataset)
    assert recovery["ok"] is True
    assert recovery["restored"] is True
    assert not stale.exists()
    empty = store.get_object_ledger_entry("obj:empty")
    assert empty["effective_level"] is None
    assert empty["decision_status"] == "NO_BASIS"
    store.close()


def test_replay_from_base_checkpoint(dataset, tmp_path):
    snapshot = tmp_path / "base-checkpoint.json"
    store = Store(":memory:")
    store.import_base(dataset)
    write_checkpoint(store, snapshot)

    recovery = recover_to_checkpoint(store, snapshot, dataset)
    assert recovery["ok"] is True
    assert len(recovery["replayed_batches"]) == len(dataset.batches)
    report = store.export_reentry_report()
    assert report["changed_object_set"] == [
        "obj:batch-add",
        "obj:mig-gain",
        "obj:mig-loss",
        "obj:mig-rebalance",
        "obj:revoke-down",
    ]
    store.close()


def test_snapshot_version_change_is_detected(dataset, tmp_path):
    snapshot = tmp_path / "checkpoint.json"
    store = Store(":memory:")
    import_dataset(store, dataset)
    write_checkpoint(store, snapshot)
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["snapshot_version"] = 999
    atomic_write_json(snapshot, payload)
    result = verify_checkpoint(store, snapshot)
    assert any(issue["code"] == "E_SNAPSHOT_VERSION" for issue in result["issues"])
    store.close()


def test_layout_version_change_rebuilds(tmp_path):
    db = tmp_path / "layout.sqlite3"
    old = Store(db, expected_layout_version="layout-v0", expected_level_enum_version="levels-v0")
    old.meta_set("custom_marker", "old")
    old.close()

    new = Store(db, expected_layout_version="layout-v1", expected_level_enum_version="levels-v1")
    assert new.layout_change is not None
    assert new.layout_change["rebuilt"] is True
    assert new.meta_get("custom_marker") is None
    assert new.meta_get("layout_version") == "layout-v1"
    new.close()


def test_atomic_write_replaces_only_valid_json(tmp_path):
    target = tmp_path / "report.json"
    atomic_write_json(target, {"ok": True})
    before = target.read_text(encoding="utf-8")

    def reject(value):
        raise ValueError("invalid")

    try:
        atomic_write_json(target, {"ok": False}, validator=reject)
    except ValueError:
        pass
    assert target.read_text(encoding="utf-8") == before
    leftovers = list(tmp_path.glob(".report.json.*.tmp"))
    assert leftovers == []


def test_checkpoint_payload_is_canonically_serializable(dataset):
    store = Store(":memory:")
    import_dataset(store, dataset)
    payload = store.build_checkpoint_payload()
    assert json.loads(canonical_json(payload))["ledger_digest"] == store.ledger_digest()
    store.close()
