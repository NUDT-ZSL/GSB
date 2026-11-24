from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from osl_forensics.checkpoint import CheckpointManager
from osl_forensics.engine import BudgetConfig, ProcessJob, process_files
from osl_forensics.errors import LockConflict
from osl_forensics.ledger import Ledger
from osl_forensics.parser import parse_osl
from osl_forensics.storage import atomic_write_json, write_snapshot
from osl_forensics.writer import SegmentSpec, build_osl


def _good_job(file_id: str, token: bytes) -> ProcessJob:
    return ProcessJob(
        file_id=file_id,
        data=build_osl([SegmentSpec("seg", token)]).data,
    )


def test_atomic_json_write_rejects_conflicting_lock(tmp_path):
    fcntl = pytest.importorskip("fcntl")
    lock_path = tmp_path / ".osl-storage.lock"
    handle = open(lock_path, "a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(LockConflict) as exc:
            atomic_write_json(tmp_path / "report.json", {"ok": True}, lock_timeout_seconds=0)
        assert exc.value.code == "E_LOCK"
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def test_snapshot_version_change_is_identified(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    atomic_write_json(snapshot, {"snapshot_version": 1, "value": "old"})
    result = write_snapshot(snapshot, {"snapshot_version": 2, "value": "new"})
    written = json.loads(snapshot.read_text(encoding="utf-8"))
    assert result["version_changed"] is True
    assert result["old_version"] == 1
    assert result["new_version"] == 2
    assert written["previous_snapshot_version"] == 1


def test_recovery_uses_last_complete_checkpoint(tmp_path):
    workdir = tmp_path / "state"
    db = tmp_path / "ledger.sqlite"
    manager = CheckpointManager(workdir)
    jobs = [_good_job("a", b"a"), _good_job("b", b"b"), _good_job("c", b"c")]

    with Ledger(db) as ledger:
        process_files(
            jobs,
            budget=BudgetConfig(concurrency=1, timeout_seconds=5, max_queue=10),
            ledger=ledger,
            checkpoint_manager=manager,
        )

    with Ledger(db) as ledger:
        extra = parse_osl(
            build_osl([SegmentSpec("later", b"uncheckpointed")]).data,
            "later",
        )
        ledger.apply_result(extra, run_id="later")

    checkpoint_dir = workdir / "checkpoints"
    (checkpoint_dir / "checkpoint-000004.json.tmp").write_text('{"complete": tr', encoding="utf-8")
    (checkpoint_dir / "checkpoint-000005.json").write_text("{corrupt", encoding="utf-8")

    recovered = manager.recover(db)
    try:
        assert recovered.recovered_from_sequence == 3
        assert recovered.restored_snapshot is True
        file_ids = {row["file_id"] for row in recovered.ledger.list_states()}
        assert file_ids == {"a", "b", "c"}
        assert recovered.ledger.verify_replay()["ok"]
    finally:
        recovered.ledger.close()


def test_forced_process_exit_recovers_checkpoint(tmp_path):
    workdir = tmp_path / "kill-state"
    db = tmp_path / "kill-ledger.sqlite"
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    for file_id, token in (("a", b"a"), ("b", b"b"), ("c", b"c")):
        (sample_dir / f"{file_id}.osl").write_bytes(
            build_osl([SegmentSpec("seg", token)]).data
        )

    code = f"""
import os
from pathlib import Path
from osl_forensics.checkpoint import CheckpointManager
from osl_forensics.engine import BudgetConfig, ProcessJob, process_files
from osl_forensics.ledger import Ledger
root = Path({str(sample_dir)!r})
jobs = [ProcessJob(file_id=p.stem, path=p) for p in sorted(root.glob('*.osl'))]
manager = CheckpointManager({str(workdir)!r})
with Ledger({str(db)!r}) as ledger:
    process_files(jobs, budget=BudgetConfig(concurrency=1), ledger=ledger,
                  checkpoint_manager=manager)
"""
    env = dict(os.environ)
    env["OSL_CRASH_AFTER_LEDGER_BEFORE_CHECKPOINT"] = "b"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert completed.returncode != 0

    manager = CheckpointManager(workdir)
    latest = manager.latest_complete()
    assert latest is not None
    assert latest["sequence"] == 1
    recovered = manager.recover(db)
    try:
        assert recovered.recovered_from_sequence == 1
        assert recovered.restored_snapshot is True
        assert {row["file_id"] for row in recovered.ledger.list_states()} == {"a"}
    finally:
        recovered.ledger.close()
