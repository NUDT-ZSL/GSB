from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tsaudit.cli import main
from tsaudit.codes import Code
from tsaudit.engine import AuditEngine
from tsaudit.errors import AuditError
from tsaudit.io_utils import WorkspaceLock


def test_lock_conflict_returns_stable_code(tmp_path: Path) -> None:
    lock_path = tmp_path / "state" / "workspace.lock"
    holder = WorkspaceLock(lock_path)
    holder.acquire()
    try:
        with pytest.raises(AuditError) as exc_info:
            AuditEngine(tmp_path / "state")
        assert exc_info.value.code == Code.E_LOCK
    finally:
        holder.release()


def test_external_process_lock_conflict(sample_dir: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    code = (
        "import time; from tsaudit.io_utils import WorkspaceLock;"
        f"lock=WorkspaceLock({str(state / 'workspace.lock')!r});"
        "lock.acquire(); print('held', flush=True); time.sleep(3)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "held"
        result = subprocess.run(
            [sys.executable, "-m", "tsaudit", "verify-checkpoint", "--workspace", str(state)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=5,
        )
        payload = json.loads(result.stdout)
        assert payload["code"] == Code.E_LOCK
        assert result.returncode == 1
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_pre_commit_failure_leaves_checkpoint_and_digest_unchanged(
    sample_dir: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = AuditEngine(workspace)
    engine.adjudicate_sample(sample_dir)
    before = engine.verify_checkpoint(recover=True)
    monkeypatch.setenv("TSAUDIT_FAULT", "before_commit_raise")
    with pytest.raises(AuditError) as exc_info:
        engine.import_update(sample_dir / "updates" / "revoke-alpha.json")
    assert exc_info.value.code == Code.E_BATCH_PRECONDITION
    after = engine.verify_checkpoint(recover=False)
    assert after["ledger_digest"] == before["ledger_digest"]
    assert after["checkpoint_seq"] == before["checkpoint_seq"]


def test_forced_kill_after_commit_recovers_consistent_checkpoint(
    sample_dir: Path, tmp_path: Path
) -> None:
    state = tmp_path / "kill-state"
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["TSAUDIT_FAULT"] = "kill_after_commit_before_checkpoint"
    killed = subprocess.run(
        [sys.executable, "-m", "tsaudit", "anchor",
         "--samples", str(sample_dir), "--workspace", str(state), "--summary-only"],
        cwd=root, env=env, capture_output=True, text=True, timeout=20,
    )
    assert killed.returncode != 0

    engine = AuditEngine(state)
    recovery = engine.verify_checkpoint(recover=True)
    assert recovery["ledger_consistent"] is True
    assert recovery["status"] in {"consistent", "recovered"}

    final = engine.adjudicate_sample(sample_dir)
    assert final["summary"]["artifact_count"] == 12
    verified = engine.verify_checkpoint(recover=False)
    assert verified["ledger_consistent"] is True


def test_torn_checkpoint_file_and_ledger_row_can_recover(
    sample_dir: Path, workspace: Path
) -> None:
    engine = AuditEngine(workspace)
    engine.adjudicate_sample(sample_dir)

    checkpoint = workspace / "checkpoint.json"
    checkpoint.write_text("{torn", encoding="utf-8")
    stale = engine.verify_checkpoint(recover=False)
    assert stale["status"] == "checkpoint_file_stale"
    recovered = engine.verify_checkpoint(recover=True)
    assert recovered["ledger_consistent"] is True

    connection = engine.store.connect()
    try:
        connection.execute("UPDATE verdicts SET status='tampered' WHERE artifact_id='art-ok'")
        connection.commit()
    finally:
        connection.close()
    mismatch = engine.verify_checkpoint(recover=False)
    assert mismatch["status"] == "ledger_mismatch"
    restored = engine.verify_checkpoint(recover=True)
    assert restored["status"] == "recovered"
    report = engine.get_report()
    art_ok = next(a for a in report["artifacts"] if a["artifact_id"] == "art-ok")
    assert art_ok["status"] == "anchored"


def test_version_change_forces_full_rebuild(
    sample_dir: Path, workspace: Path
) -> None:
    engine = AuditEngine(workspace)
    engine.adjudicate_sample(sample_dir)
    connection = engine.store.connect()
    try:
        connection.execute(
            "UPDATE meta SET value='999' WHERE key='merge_policy_version'"
        )
        connection.commit()
    finally:
        connection.close()

    restarted = AuditEngine(workspace)
    check = restarted.verify_checkpoint(recover=False)
    assert check["code"] == Code.E_VERSION_REBUILD
    rebuilt = restarted.verify_checkpoint(recover=True)
    assert rebuilt["status"] == "rebuilt"
    assert rebuilt["ledger_consistent"] is True


def test_cli_exit_codes_report_and_checkpoint(
    sample_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "cli-state"
    output = tmp_path / "report.json"
    exit_code = main([
        "anchor", "--samples", str(sample_dir), "--workspace", str(state),
        "--output", str(output),
    ])
    assert exit_code == 1  # generated sample intentionally contains bad records
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["artifact_count"] == 12
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["format"] == "tsaudit.audit-report"

    assert main(["verify-checkpoint", "--workspace", str(state)]) == 0
    exported = tmp_path / "export.json"
    export_exit = main([
        "export-report", "--workspace", str(state), "--output", str(exported)
    ])
    assert export_exit == 1
    assert json.loads(exported.read_text(encoding="utf-8"))["artifacts"]


def test_cli_parameter_error_uses_exit_code_2() -> None:
    assert main(["anchor", "--definitely-not-an-option"]) == 2
