from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from conftest import ROOT, cli_json, run_cli, samples_dir


def _prepare(workspace: Path, samples: Path) -> None:
    assert cli_json(
        "import-events",
        "--workspace",
        workspace,
        "--events",
        samples / "events.jsonl",
    )["exit_code"] == 0
    assert cli_json(
        "import-rules",
        "--workspace",
        workspace,
        "--snapshot",
        samples / "rules_snapshot.json",
    )["exit_code"] == 0
    assert cli_json("recompute", "--workspace", workspace)["exit_code"] == 0


def test_checkpoint_verify_tamper_and_restore(tmp_path, samples_dir):
    workspace = tmp_path / "ws"
    _prepare(workspace, samples_dir)
    created = cli_json(
        "checkpoint",
        "create",
        "--workspace",
        workspace,
    )
    assert created["exit_code"] == 0
    verified = cli_json(
        "checkpoint",
        "verify",
        "--workspace",
        workspace,
    )
    assert verified["exit_code"] == 0
    assert verified["current_ledger_matches_latest"] is True

    connection = sqlite3.connect(workspace / "audit_masker.sqlite3")
    try:
        rowid = connection.execute("SELECT MIN(rowid) FROM ledger").fetchone()[0]
        connection.execute(
            "UPDATE ledger SET masked_value = 'TAMPERED' WHERE rowid = ?",
            (rowid,),
        )
        connection.commit()
    finally:
        connection.close()

    diverged = cli_json(
        "checkpoint",
        "verify",
        "--workspace",
        workspace,
    )
    assert diverged["current_ledger_matches_latest"] is False
    assert diverged["business_codes"]["W_LEDGER_DIVERGED"] == 1
    restored = cli_json(
        "checkpoint",
        "restore",
        "--workspace",
        workspace,
    )
    assert restored["exit_code"] == 0
    verified_again = cli_json(
        "checkpoint",
        "verify",
        "--workspace",
        workspace,
    )
    assert verified_again["current_ledger_matches_latest"] is True
    assert cli_json("verify-full", "--workspace", workspace)["mismatch_count"] == 0


def test_forced_termination_leaves_only_orphan_snapshot(tmp_path, samples_dir):
    workspace = tmp_path / "ws"
    _prepare(workspace, samples_dir)
    assert cli_json(
        "checkpoint",
        "create",
        "--workspace",
        workspace,
    )["exit_code"] == 0

    crashed = run_cli(
        "checkpoint",
        "create",
        "--workspace",
        workspace,
        "--snapshot",
        workspace / "checkpoints" / "crashed.json",
        env={"AUDIT_MASKER_FAULT": "after_snapshot"},
    )
    assert crashed.returncode == 137
    assert crashed.stdout == ""
    assert crashed.stderr == ""

    verified = cli_json(
        "checkpoint",
        "verify",
        "--workspace",
        workspace,
    )
    assert verified["exit_code"] == 0
    assert verified["valid_count"] == 1
    assert verified["orphan_snapshot_count"] >= 1
    assert any(
        item.endswith("crashed.json")
        for item in verified["orphan_snapshots"]
    )
    assert cli_json(
        "checkpoint",
        "restore",
        "--workspace",
        workspace,
    )["exit_code"] == 0


def test_restore_allows_newer_rules_then_recompute(tmp_path, samples_dir):
    workspace = tmp_path / "ws"
    _prepare(workspace, samples_dir)
    assert cli_json(
        "checkpoint",
        "create",
        "--workspace",
        workspace,
    )["exit_code"] == 0
    assert cli_json(
        "import-rules",
        "--workspace",
        workspace,
        "--patch",
        samples_dir / "patch-002-auth-tombstone.json",
    )["exit_code"] == 0
    restored = cli_json(
        "checkpoint",
        "restore",
        "--workspace",
        workspace,
    )
    assert restored["exit_code"] == 0
    assert restored["rules_changed_since_checkpoint"] is True
    assert restored["business_codes"]["W_RECOMPUTE_REQUIRED"] == 1
    recomputed = cli_json("recompute", "--workspace", workspace)
    assert recomputed["exit_code"] == 0
    assert recomputed["impacted_event_count"] > 0
    assert cli_json("verify-full", "--workspace", workspace)["mismatch_count"] == 0


def test_lock_conflict_returns_e_lock(tmp_path, samples_dir):
    workspace = tmp_path / "ws"
    script = (
        "import time\n"
        "from audit_masker.locking import WorkspaceLock\n"
        f"with WorkspaceLock({str(workspace)!r}):\n"
        "    print('LOCKED', flush=True)\n"
        "    time.sleep(5)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "LOCKED"
        result = cli_json(
            "import-events",
            "--workspace",
            workspace,
            "--events",
            samples_dir / "events.jsonl",
        )
        assert result["exit_code"] == 1
        assert result["business_codes"] == {"E_LOCK": 1}
    finally:
        holder.terminate()
        holder.wait(timeout=5)
