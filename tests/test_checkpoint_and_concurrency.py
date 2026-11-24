from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from certpath.engine import CertPathEngine
from certpath.errors import CertPathError
from certpath.store import WorkspaceLock
from tests.helpers import load_json, run_python


ROOT = Path(__file__).resolve().parents[1]


def _adjudicated(tmp_path: Path, sample_dir: Path) -> CertPathEngine:
    engine = CertPathEngine(tmp_path / "state")
    engine.adjudicate(sample_dir)
    return engine


def test_checkpoint_is_complete_and_ledger_consistent(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    engine = _adjudicated(tmp_path, sample_dir)
    result = engine.verify_checkpoint(recover=True)
    assert result["status"] == "consistent"
    assert result["ledger_consistent"] is True
    assert result["checkpoint_seq"] >= 3
    checkpoint = load_json(engine.store.checkpoint_path)
    assert checkpoint["format"] == "certpath.checkpoint"
    assert checkpoint["complete"] is True
    assert checkpoint["checkpoint_seq"] == result["checkpoint_seq"]
    ledger_snapshot = load_json(engine.store.ledger_snapshot_path)
    assert ledger_snapshot["ledger_digest"] == result["ledger_digest"]


def test_lock_conflict_returns_e_lock_without_damage(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    engine = _adjudicated(tmp_path, sample_dir)
    before = engine.store.current_ledger_digest()
    lock_path = engine.store.lock_path
    holder_code = (
        "import sys\n"
        "from certpath.store import WorkspaceLock\n"
        "lock=WorkspaceLock(sys.argv[1]); lock.acquire()\n"
        "print('READY', flush=True)\n"
        "sys.stdin.read()\n"
        "lock.release()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(lock_path)],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        with pytest.raises(CertPathError) as captured:
            engine.adjudicate(sample_dir)
        assert captured.value.code == "E_LOCK"
        assert engine.store.current_ledger_digest() == before
    finally:
        assert process.stdin is not None
        process.stdin.close()
        process.wait(timeout=10)
    assert process.returncode == 0


def test_forced_termination_after_commit_recovers_last_checkpoint(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    engine = _adjudicated(tmp_path, sample_dir)
    before = engine.verify_checkpoint(recover=True)
    code = (
        "import sys\n"
        "from certpath.engine import CertPathEngine\n"
        "CertPathEngine(sys.argv[1]).adjudicate(sys.argv[2], reimport=False)\n"
    )
    exit_code, _, _ = run_python(
        code,
        str(engine.workspace),
        str(sample_dir),
        env={"CERTPATH_FAULT": "after_commit_before_checkpoint"},
    )
    assert exit_code == 91
    result = engine.verify_checkpoint(recover=True)
    assert result["ledger_consistent"] is True
    assert result["checkpoint_seq"] == before["checkpoint_seq"] + 1
    assert result["checkpoint_file_seq"] == result["checkpoint_seq"]
    checkpoint = load_json(engine.store.checkpoint_path)
    assert checkpoint["checkpoint_seq"] == result["checkpoint_seq"]
    assert len(engine.store.load_verdict_rows()) == 13


def test_forced_termination_inside_transaction_keeps_previous_checkpoint(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    engine = _adjudicated(tmp_path, sample_dir)
    before = engine.verify_checkpoint(recover=True)
    code = (
        "import sys\n"
        "from certpath.engine import CertPathEngine\n"
        "CertPathEngine(sys.argv[1]).adjudicate(sys.argv[2], reimport=False)\n"
    )
    exit_code, _, _ = run_python(
        code,
        str(engine.workspace),
        str(sample_dir),
        env={"CERTPATH_FAULT": "inside_transaction_before_commit"},
    )
    assert exit_code == 91
    result = engine.verify_checkpoint(recover=True)
    assert result["checkpoint_seq"] == before["checkpoint_seq"]
    assert result["ledger_digest"] == before["ledger_digest"]
    assert len(engine.store.load_verdict_rows()) == 13


def test_half_written_ledger_is_detected_and_replayed_from_checkpoint(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    engine = _adjudicated(tmp_path, sample_dir)
    expected = engine.verify_checkpoint(recover=True)
    connection = sqlite3.connect(engine.store.db_path)
    try:
        connection.execute("DELETE FROM verdicts WHERE chain_id='chain-cross'")
        connection.commit()
    finally:
        connection.close()
    residue = engine.verify_checkpoint(recover=False)
    assert residue["status"] == "residue"
    assert residue["ledger_consistent"] is False
    repaired = engine.verify_checkpoint(recover=True)
    assert repaired["recovered"] is True
    assert repaired["ledger_digest"] == expected["ledger_digest"]
    assert len(engine.store.load_verdict_rows()) == 13


def test_corrupt_external_checkpoint_falls_back_to_sqlite_checkpoint(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    engine = _adjudicated(tmp_path, sample_dir)
    engine.store.checkpoint_path.write_text('{"truncated":', encoding="utf-8")
    result = engine.verify_checkpoint(recover=True)
    assert result["ledger_consistent"] is True
    assert result["checkpoint_file_refreshed"] is True
    repaired = json.loads(engine.store.checkpoint_path.read_text(encoding="utf-8"))
    assert repaired["complete"] is True


def test_report_can_be_reconstructed_from_ledger(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    engine = _adjudicated(tmp_path, sample_dir)
    engine.store.report_path.unlink()
    output = tmp_path / "reconstructed.json"
    report = engine.export_report(output)
    assert report["format"] == "certpath.report"
    assert len(report["chains"]) == 13
    assert report["summary"]["candidate_paths_total"] == 24
    assert load_json(output)["chains"] == report["chains"]


def test_corrupt_trust_view_is_restored_from_checkpoint(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    engine = _adjudicated(tmp_path, sample_dir)
    engine.store.trust_view_path.write_text('{"broken":', encoding="utf-8")
    detected = engine.verify_checkpoint(recover=False)
    assert detected["status"] == "artifacts_stale"
    assert detected["ledger_consistent"] is True
    repaired = engine.verify_checkpoint(recover=True)
    assert repaired["artifacts_consistent"] is True
    view = load_json(engine.store.trust_view_path)
    assert view["format"] == "certpath.trust-view"


def test_workspace_lock_is_non_blocking(tmp_path: Path) -> None:
    lock_path = tmp_path / "single.lock"
    first = WorkspaceLock(lock_path)
    first.acquire()
    try:
        with pytest.raises(CertPathError) as captured:
            WorkspaceLock(lock_path).acquire()
        assert captured.value.code == "E_LOCK"
    finally:
        first.release()


def test_checkpoint_schema_version_change_forces_rebuild(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    engine = _adjudicated(tmp_path, sample_dir)
    connection = sqlite3.connect(engine.store.db_path)
    try:
        connection.execute(
            "UPDATE meta SET value='999' WHERE key='schema_version'"
        )
        connection.commit()
    finally:
        connection.close()
    rebuilt = CertPathEngine(engine.workspace)
    assert rebuilt.store.load_verdict_rows() == []
    assert rebuilt.store.read_meta()["schema_version"] == "1"
    assert rebuilt.verify_checkpoint(recover=True)["status"] == "empty"
    assert not rebuilt.store.report_path.exists()
