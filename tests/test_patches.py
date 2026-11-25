from __future__ import annotations

import sqlite3

from health_machine.cli import main
from health_machine.patches import apply_patch_file
from health_machine.status import build_status
from health_machine.store import Store
from health_machine.verify import verify_store


def backup_database(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def state_hashes(db):
    conn = sqlite3.connect(db)
    try:
        return dict(conn.execute("SELECT instance_id, state_hash FROM instance_state ORDER BY instance_id").fetchall())
    finally:
        conn.close()


def test_threshold_patch_closure_idempotency_and_reuse(completed_workspace):
    patched_db = backup_database(completed_workspace.db, completed_workspace.root / "patch.sqlite")
    before = state_hashes(patched_db)
    store = Store(patched_db)
    patch_file = completed_workspace.samples / "patches" / "01_adjust_flap.json"

    result = apply_patch_file(store, patch_file)
    assert result["idempotent"] is False
    assert result["recomputed"][0]["instance_id"] == "inst-flap"
    assert set(result["reusable_instances"]) == {"inst-outage", "inst-steady", "inst-timeout"}

    after = state_hashes(patched_db)
    assert after["inst-steady"] == before["inst-steady"]
    assert after["inst-outage"] == before["inst-outage"]
    assert after["inst-timeout"] == before["inst-timeout"]

    conn = sqlite3.connect(patched_db)
    audit = conn.execute("SELECT instance_id, window_count FROM recompute_audit").fetchall()
    assert audit == [("inst-flap", 6)]
    conn.close()

    duplicate = apply_patch_file(Store(patched_db), patch_file)
    assert duplicate["idempotent"] is True
    conn = sqlite3.connect(patched_db)
    assert conn.execute("SELECT COUNT(*) FROM recompute_audit").fetchone()[0] == 1
    conn.close()

    verification = verify_store(Store(patched_db))
    assert verification["ok"], verification


def test_rename_patch_moves_full_reference_closure(completed_workspace):
    renamed_db = backup_database(completed_workspace.db, completed_workspace.root / "rename.sqlite")
    assert main([
        "patch", "--db", str(renamed_db),
        "--file", str(completed_workspace.samples / "patches" / "02_rename_steady.json"),
    ]) == 0
    conn = sqlite3.connect(renamed_db)
    assert conn.execute("SELECT 1 FROM instances WHERE instance_id='inst-steady'").fetchone() is None
    assert conn.execute("SELECT state FROM instance_state WHERE instance_id='inst-steady-renamed'").fetchone()[0] == "healthy"
    assert conn.execute("SELECT COUNT(*) FROM observation_inbox WHERE instance_id='inst-steady-renamed'").fetchone()[0] == 15
    closure = conn.execute(
        "SELECT DISTINCT scope FROM patch_closures WHERE patch_id='patch-rename-steady' ORDER BY scope"
    ).fetchall()
    assert len(closure) == 5
    conn.close()
    verification = verify_store(Store(renamed_db))
    assert verification["ok"], verification


def test_tombstone_patch_excludes_level_and_verifies(completed_workspace):
    tomb_db = backup_database(completed_workspace.db, completed_workspace.root / "tomb.sqlite")
    result = apply_patch_file(
        Store(tomb_db), completed_workspace.samples / "patches" / "03_tombstone_timeout_level3.json"
    )
    assert result["recomputed"][0]["instance_id"] == "inst-timeout"
    status = build_status(Store(tomb_db), "inst-timeout")["instances"][0]
    excluded = {(item["level"], item["reason"]) for item in status["excluded_thresholds"]}
    assert (3, "tombstoned") in excluded
    assert verify_store(Store(tomb_db))["ok"]


def test_duplicate_merge_version_is_idempotent(workspace):
    assert main(["merge", "--samples", str(workspace.samples), "--db", str(workspace.db)]) == 0
    conn = sqlite3.connect(workspace.db)
    assert conn.execute("SELECT COUNT(*) FROM snapshot_versions WHERE snapshot_version='fixed-sample'").fetchone()[0] == 1
    conn.close()
