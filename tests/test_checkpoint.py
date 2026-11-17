from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from slpguard import (
    BatchConfig,
    CheckpointState,
    CheckpointStore,
    ErrorCode,
    FileLock,
    SLPError,
    run_batch,
)
from slpguard.io_utils import atomic_write_json, read_json_envelope


ROOT = Path(__file__).resolve().parents[1]


def _store(tmp_path: Path, **kwargs) -> CheckpointStore:
    return CheckpointStore(
        tmp_path,
        config_fingerprint="test-config",
        **kwargs,
    )


def test_atomic_checkpoint_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = {"file_id": "abc", "status": "ok"}
    state = CheckpointState(completed={"abc": result})
    store.save(state)
    loaded = store.load()
    assert loaded.completed == {"abc": result}
    assert loaded.rebuilt is False
    payload = read_json_envelope(store.path, kind="slp-checkpoint")
    assert payload["aggregate"]["files"] == 1
    assert list((tmp_path / "checkpoints").glob("*.tmp")) == []


def test_schema_and_phase_version_changes_rebuild(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(CheckpointState(completed={"abc": {"file_id": "abc", "status": "ok"}}))

    # Rewrite a valid envelope carrying a future schema version.
    payload = read_json_envelope(store.path, kind="slp-checkpoint")
    payload["checkpoint_schema_version"] = 999
    atomic_write_json(store.path, payload, kind="slp-checkpoint")
    rebuilt = store.load()
    assert rebuilt.rebuilt is True
    assert "schema changed" in (rebuilt.rebuild_reason or "")
    assert rebuilt.completed == {}

    store.save(CheckpointState(completed={"abc": {"file_id": "abc", "status": "ok"}}))
    phase_store = CheckpointStore(
        tmp_path,
        config_fingerprint="test-config",
        phase_table_version=2,
    )
    phase_rebuilt = phase_store.load()
    assert phase_rebuilt.rebuilt is True
    assert "phase table changed" in (phase_rebuilt.rebuild_reason or "")
    assert phase_rebuilt.completed == {}


def test_lock_conflict_is_e_lock_and_preserves_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(CheckpointState(completed={"abc": {"file_id": "abc", "status": "ok"}}))
    before = store.current_digest()

    held = store.lock().acquire()
    try:
        with pytest.raises(SLPError) as error:
            store.save(
                CheckpointState(
                    completed={"def": {"file_id": "def", "status": "ok"}}
                )
            )
        assert error.value.code is ErrorCode.E_LOCK
    finally:
        held.release()

    assert store.current_digest() == before
    assert "abc" in store.load().completed


def test_batch_reports_lock_conflict_without_overwriting(
    corpus,
    tmp_path: Path,
) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    store = CheckpointStore(workdir, config_fingerprint="seed")
    store.save(
        CheckpointState(completed={"seed": {"file_id": "seed", "status": "ok"}})
    )
    before = store.current_digest()
    held = store.lock().acquire()
    try:
        report = run_batch(
            corpus / "valid_slp.slp",
            config=BatchConfig(workers=1),
            workdir=workdir,
        )
    finally:
        held.release()
    assert any(
        error["code"] == "E_LOCK"
        for error in report["batch"]["checkpoint_errors"]
    )
    assert store.current_digest() == before


def test_corrupt_envelope_is_detected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(CheckpointState(completed={"abc": {"file_id": "abc", "status": "ok"}}))
    document = json.loads(store.path.read_text(encoding="utf-8"))
    document["payload"]["completed"]["abc"]["status"] = "tampered"
    store.path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SLPError) as error:
        store.load()
    assert error.value.code is ErrorCode.E_CHECKPOINT


def test_forced_termination_recovers_last_complete_checkpoint(
    corpus,
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    for index, name in enumerate(
        ("valid_slp.slp", "valid_v2.slp", "physical_reorder.slp")
    ):
        data = (corpus / name).read_bytes()
        (input_dir / f"{index}-{name}").write_bytes(data)

    workdir = tmp_path / "work"
    workdir.mkdir()
    script = f"""
import os, sys
sys.path.insert(0, {str(ROOT)!r})
from slpguard import BatchConfig, run_batch

def stop_after_checkpoint(result):
    os._exit(73)

run_batch(
    {str(input_dir)!r},
    config=BatchConfig(workers=1, checkpoint_enabled=True),
    workdir={str(workdir)!r},
    on_file_complete=stop_after_checkpoint,
)
"""
    killed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert killed.returncode == 73, killed.stderr
    checkpoint_path = workdir / "checkpoints" / "checkpoint.json"
    assert checkpoint_path.exists()
    assert not (workdir / "reports" / "slp-report.json").exists()
    checkpoint = read_json_envelope(checkpoint_path, kind="slp-checkpoint")
    assert checkpoint["aggregate"]["files"] >= 1

    report = run_batch(
        input_dir,
        config=BatchConfig(workers=4, checkpoint_enabled=True),
        workdir=workdir,
    )
    assert report["batch"]["files"] == 3
    assert report["batch"]["checkpoint_reused_files"] >= 1
    assert all(item["status"] == "ok" for item in report["files"])
