from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from blockstore.engine import SnapshotEngine

from .conftest import SAMPLES, read_cli_json, run_cli


def _bootstrap(tmp_path: Path, *, base_only: bool = False) -> Path:
    root = tmp_path / "store"
    args = [
        "load",
        "--volume",
        str(SAMPLES / "volume.json"),
    ]
    if not base_only:
        args.extend(
            [
                "--snapshots",
                str(SAMPLES / "snapshots.json"),
                "--rollbacks",
                str(SAMPLES / "rollbacks.json"),
            ]
        )
    else:
        args.append("--base-only")
    result = run_cli(root, *args)
    assert result.returncode == 0, result.stderr or result.stdout
    return root


def test_snapshot_interruption_resumes_prepared_run(tmp_path: Path) -> None:
    root = _bootstrap(tmp_path, base_only=True)
    crashed = run_cli(
        root,
        "snapshot",
        "--file",
        str(SAMPLES / "snapshots.json"),
        "--snapshot-id",
        "snap-a1",
        env={"BLOCKSTORE_CRASH_POINT": "snapshot:materialized"},
    )
    assert crashed.returncode == 97
    before = read_cli_json(run_cli(root, "status"))
    assert before["view_version"] == 1
    assert [item["snapshot_id"] for item in before["snapshots"]] == [
        "vol-demo@base"
    ]
    assert before["pending_operations"]["snapshots"] == 1
    connection = sqlite3.connect(root / "state.sqlite3")
    try:
        transaction_refs = connection.execute(
            "SELECT COUNT(*) FROM transaction_refs"
        ).fetchone()[0]
    finally:
        connection.close()
    assert transaction_refs == 8
    resumed = read_cli_json(run_cli(root, "resume"))
    assert resumed["status"]["view_version"] == 2
    assert [item["snapshot_id"] for item in resumed["status"]["snapshots"]] == [
        "vol-demo@base",
        "snap-a1",
    ]
    assert resumed["status"]["pending_operations"] == {
        "snapshots": 0,
        "rollbacks": 0,
        "gc": 0,
    }
    connection = sqlite3.connect(root / "state.sqlite3")
    try:
        transaction_refs = connection.execute(
            "SELECT COUNT(*) FROM transaction_refs"
        ).fetchone()[0]
    finally:
        connection.close()
    assert transaction_refs == 0
    verified = read_cli_json(run_cli(root, "verify"))
    assert verified["ok"]


def test_rollback_interruption_keeps_old_view_until_resume(tmp_path: Path) -> None:
    root = _bootstrap(tmp_path)
    crashed = run_cli(
        root,
        "rollback",
        "--file",
        str(SAMPLES / "rollbacks.json"),
        "--request-id",
        "rb-main",
        env={"BLOCKSTORE_CRASH_POINT": "rollback:prepared"},
    )
    assert crashed.returncode == 97
    before = read_cli_json(run_cli(root, "status"))
    assert before["view_version"] == 6
    assert before["anchor_snapshot_id"] == "vol-demo@base"
    assert all(item["status"] == "live" for item in before["snapshots"])
    assert before["pending_operations"]["rollbacks"] == 1
    verified = read_cli_json(run_cli(root, "verify"))
    assert verified["ok"]
    assert verified["view_version"] == 7
    assert verified["live_snapshot_count"] == 3
    assert verified["orphan_snapshot_count"] == 3
    assert verified["recovery"][0]["kind"] == "complete_rollback"
    assert verified["checks"][-1]["name"] == "no_uncommitted_transaction_refs"
    assert verified["checks"][-1]["ok"]
    assert verified["checks"][-2]["name"] == "no_unfinished_operations"
    assert verified["checks"][-2]["ok"]


def test_gc_interruption_is_completed_without_deleting_referenced_blocks(
    tmp_path: Path,
) -> None:
    root = _bootstrap(tmp_path)
    rollback = run_cli(
        root,
        "rollback",
        "--file",
        str(SAMPLES / "rollbacks.json"),
        "--request-id",
        "rb-main",
    )
    assert rollback.returncode == 0
    crashed = run_cli(
        root,
        "gc",
        env={"BLOCKSTORE_CRASH_POINT": "gc:after_one_delete"},
    )
    assert crashed.returncode == 97
    before = read_cli_json(run_cli(root, "status"))
    assert before["pending_operations"]["gc"] == 1
    resumed = read_cli_json(run_cli(root, "resume"))
    assert resumed["status"]["pending_operations"]["gc"] == 0
    assert resumed["recovery_actions"][0]["kind"] == "complete_gc"
    repeated = read_cli_json(run_cli(root, "gc"))
    assert repeated["idempotent"]
    assert repeated["deleted_count"] == 5
    verified = read_cli_json(run_cli(root, "verify"))
    assert verified["ok"]
    assert verified["distinct_referenced_blocks"] == 11


def test_verify_triggers_recovery_and_is_deterministic(tmp_path: Path) -> None:
    root = _bootstrap(tmp_path)
    crashed = run_cli(
        root,
        "rollback",
        "--file",
        str(SAMPLES / "rollbacks.json"),
        "--request-id",
        "rb-main",
        env={"BLOCKSTORE_CRASH_POINT": "rollback:prepared"},
    )
    assert crashed.returncode == 97
    first = read_cli_json(run_cli(root, "verify"))
    second = read_cli_json(run_cli(root, "verify"))
    third = read_cli_json(run_cli(root, "verify"))
    assert first["ok"] and second["ok"] and third["ok"]
    assert first["recovery"]
    assert json.dumps(second, sort_keys=True) == json.dumps(third, sort_keys=True)
