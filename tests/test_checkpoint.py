from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from telemetry_drift.checkpoint import StageStateMachine
from telemetry_drift.errors import DriftError, E_CHECKPOINT_CORRUPT, E_STAGE_BUSY
from telemetry_drift.locking import FileCommitLock


def test_checkpoint_commit_savepoint_and_exit_resume(tmp_path):
    path = tmp_path / "checkpoint.json"
    lock = tmp_path / "commit.lock"
    state = StageStateMachine(path, lock_path=lock)
    first = state.commit_stage("ingest", records=("r1",))
    second = state.commit_stage("align", records=("r2",))
    assert second["version"] == first["version"] + 1
    saved = state.savepoint("stable")
    state.enter_exit_report("SIGTERM")
    resumed = state.resume()
    assert resumed["exit_requested"] is True
    restored = state.restore_savepoint("stable")
    assert restored["state"] == "running"
    assert restored["version"] > saved["checkpoint"]["version"]
    assert restored["completed_stages"] == ["ingest", "align"]


def test_half_written_temp_never_becomes_visible(tmp_path):
    path = tmp_path / "checkpoint.json"
    state = StageStateMachine(path, lock_path=tmp_path / "commit.lock")
    committed = state.commit_stage("ingest", records=("r1",))
    (tmp_path / f".{path.name}.killed.tmp").write_text(
        '{"version":999}', encoding="utf-8"
    )
    resumed = state.resume()
    assert resumed["version"] == committed["version"]
    assert resumed["checksum"] == committed["checksum"]


def test_corrupt_checkpoint_returns_stable_error(tmp_path):
    path = tmp_path / "checkpoint.json"
    state = StageStateMachine(path, lock_path=tmp_path / "commit.lock")
    state.commit_stage("ingest", records=("r1",))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["completed_records"].append("tampered")
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DriftError) as raised:
        state.resume()
    assert raised.value.code == E_CHECKPOINT_CORRUPT


def test_commit_lock_is_non_blocking_across_processes(tmp_path):
    lock_path = tmp_path / "commit.lock"
    script = (
        "import time\n"
        "from telemetry_drift.locking import FileCommitLock\n"
        f"lock=FileCommitLock({str(lock_path)!r})\n"
        "lock.acquire()\n"
        "print('locked', flush=True)\n"
        "time.sleep(0.8)\n"
    )
    environment = dict(os.environ)
    root = str(tmp_path.parents[1])
    environment["PYTHONPATH"] = os.getcwd()
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        state = StageStateMachine(
            tmp_path / "state.json", lock_path=lock_path
        )
        with pytest.raises(DriftError) as raised:
            state.commit_stage("ingest")
        assert raised.value.code == E_STAGE_BUSY
    finally:
        process.wait(timeout=5)

