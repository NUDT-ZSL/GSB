from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sandbox_guard.errors import GuardError
from sandbox_guard.locking import FileLock
from sandbox_guard.parsing import parse_policy
from sandbox_guard.workspace import Workspace


def seeded_workspace(tmp_path: Path, sample_dir: Path) -> Workspace:
    policy = parse_policy(json.loads((sample_dir / "policy.json").read_text("utf-8")))
    ws = Workspace(tmp_path / "work")
    ws.initialize_policy(policy)
    for patch in sorted((sample_dir / "patches").glob("*.json")):
        result = ws.import_batch_file(patch)
        assert result["status"] == "applied"
    return ws


def test_checkpoint_snapshot_recovers_from_torn_file_and_residue(tmp_path, sample_dir):
    ws = seeded_workspace(tmp_path, sample_dir)
    before = ws.verify_checkpoint()
    assert before["ok"] is True
    digest = before["ledger_digest"]

    ws.snapshot_path.write_text('{"checkpoint_id": "cp-5", "ledger_digest": "broken', encoding="utf-8")
    residue = ws.root / ".checkpoint.json.tmp-999-1"
    residue.write_text("{partial", encoding="utf-8")

    invalid = ws.verify_checkpoint()
    assert invalid["ok"] is False
    assert invalid["code"] in {"E_SNAPSHOT_CORRUPT", "E_CHECKPOINT_CORRUPT"}
    assert ".checkpoint.json.tmp-999-1" in invalid["residue_files"]

    recovery = ws.recover()
    assert recovery["ok"] is True
    assert recovery["snapshot_status"] == "restored"
    assert recovery["residue_files"][0]["action"] == "removed_temp"
    assert not residue.exists()

    after = ws.verify_checkpoint()
    assert after["ok"] is True
    assert after["ledger_digest"] == digest
    snapshot = json.loads(ws.snapshot_path.read_text("utf-8"))
    assert snapshot["ledger_digest"] == digest


def test_recovery_chooses_last_complete_sqlite_checkpoint(tmp_path, sample_dir):
    ws = seeded_workspace(tmp_path, sample_dir)
    good = ws.recover()
    assert good["checkpoint_id"] == "cp-5"

    with sqlite3.connect(ws.db_path) as conn:
        conn.execute(
            "INSERT INTO checkpoints(checkpoint_id, checkpoint_seq, apply_watermark, "
            "ledger_digest, complete, payload_json) VALUES(?, ?, ?, ?, 0, ?)",
            ("cp-999-torn", 999, 999, "bad-digest", json.dumps({"checkpoint_id": "cp-999-torn"})),
        )
        conn.commit()

    recovery = ws.recover()
    assert recovery["checkpoint_id"] == "cp-5"
    assert any(item["checkpoint_id"] == "cp-999-torn" for item in recovery["invalid_checkpoints"])
    assert ws.verify_checkpoint()["ok"] is True


def test_version_change_requires_rebuild(tmp_path, sample_dir):
    ws = seeded_workspace(tmp_path, sample_dir)
    snapshot = json.loads(ws.snapshot_path.read_text("utf-8"))
    snapshot["merge_policy_version"] = "merge-policy-v0-old"
    ws.snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    with pytest.raises(GuardError) as exc_info:
        ws.recover()
    assert exc_info.value.code == "E_VERSION_REBUILD"
    assert exc_info.value.details["rebuild_required"] is True or "snapshot_versions" in exc_info.value.details


def test_recovery_replays_committed_operations_after_last_checkpoint(tmp_path, sample_dir):
    ws = seeded_workspace(tmp_path, sample_dir)
    with sqlite3.connect(ws.db_path) as conn:
        conn.execute("DELETE FROM checkpoints WHERE checkpoint_id='cp-5'")
        conn.commit()

    recovery = ws.recover()
    assert recovery["last_complete_checkpoint_id"] == "cp-4"
    assert recovery["replayed_operation_count"] == 1
    assert recovery["checkpoint_id"] == "cp-5"
    assert ws.verify_checkpoint()["ok"] is True


def test_lock_conflict_returns_e_lock_and_preserves_checkpoint(tmp_path, sample_dir):
    ws = seeded_workspace(tmp_path, sample_dir)
    before = ws.verify_checkpoint()
    lock = FileLock(ws.lock_path, timeout_ms=0)
    lock.acquire()
    try:
        with pytest.raises(GuardError) as exc_info:
            ws.create_checkpoint(timeout_ms=0)
        assert exc_info.value.code == "E_LOCK"
    finally:
        lock.release()
    after = ws.verify_checkpoint()
    assert after["ok"] is True
    assert after["ledger_digest"] == before["ledger_digest"]


def test_concurrent_checkpoints_serialize_with_lock(tmp_path, sample_dir):
    from concurrent.futures import ThreadPoolExecutor

    ws = seeded_workspace(tmp_path, sample_dir)

    def write_one():
        return ws.create_checkpoint(timeout_ms=5_000)["checkpoint_id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: write_one(), range(6)))

    assert results == ["cp-5"] * 6
    assert ws.verify_checkpoint()["ok"] is True
