from __future__ import annotations

import json
from pathlib import Path

import pytest

from alert_engine.checkpoint import CheckpointManager
from alert_engine.errors import EngineError
from alert_engine.util import FileLock
from conftest import run_sample


def test_resume_uses_last_complete_checkpoint(sample_dir, tmp_path):
    workspace = tmp_path / "resume"
    engine, first = run_sample(sample_dir, workspace, workers=2)
    _, resumed = run_sample(
        sample_dir, workspace, workers=2, resume=True
    )
    assert resumed["checkpoint"]["restored"] is True
    assert resumed["checkpoint"]["resume_requested"] is True
    assert resumed["counts"]["clusters"] == first["counts"]["clusters"]
    assert resumed["counts"]["patch_batches_applied"] == 4
    assert resumed["diagnostics"]["recompute"]["full_match"] is True


def test_corrupt_latest_snapshot_falls_back_and_reports_residual(
    sample_dir, tmp_path
):
    workspace = tmp_path / "corrupt"
    engine, first = run_sample(sample_dir, workspace)
    snapshot_name = first["checkpoint"]["latest_saved"]["snapshot_name"]
    snapshot_path = workspace / "checkpoints" / snapshot_name
    snapshot_path.write_text('{"half":', encoding="utf-8")

    _, resumed = run_sample(
        sample_dir, workspace, resume=True
    )
    assert resumed["status"] == "OK"
    assert resumed["checkpoint"]["restored"] is True
    residual_codes = {
        item["code"] for item in resumed["checkpoint"]["load_residuals"]
    }
    assert "E_CHECKPOINT_CORRUPT" in residual_codes
    assert resumed["counts"]["clusters"] == first["counts"]["clusters"]


def test_checkpoint_structure_version_forces_full_rebuild(
    sample_dir, tmp_path
):
    workspace = tmp_path / "structure"
    engine, first = run_sample(sample_dir, workspace)
    manager = CheckpointManager(
        db_path=workspace / "engine.sqlite3",
        checkpoint_dir=workspace / "checkpoints",
        temp_dir=workspace / "tmp",
        lock_path=workspace / ".engine.lock",
    )
    try:
        manager.connection.execute(
            "UPDATE checkpoints SET structure_version=999 "
            "WHERE seq=(SELECT MAX(seq) FROM checkpoints)"
        )
        loaded = manager.load_latest(
            input_fingerprint=first["input_fingerprint"]
        )
        assert loaded.rebuild_required is True
        assert loaded.rebuild_reason == "E_CHECKPOINT_REBUILD"
        assert loaded.payload is None
    finally:
        manager.close()


def test_half_written_temp_is_reported(default_report):
    engine, report = default_report
    (engine.temp_dir / "orphan-checkpoint.tmp").write_text(
        "not-complete", encoding="utf-8"
    )
    manager = CheckpointManager(
        db_path=engine.db_path,
        checkpoint_dir=engine.checkpoint_dir,
        temp_dir=engine.temp_dir,
        lock_path=engine.lock_path,
    )
    try:
        loaded = manager.load_latest(
            input_fingerprint=report["input_fingerprint"]
        )
        assert loaded.found
        assert any(
            path.endswith("orphan-checkpoint.tmp")
            for path in loaded.orphan_temp_files
        )
    finally:
        manager.close()


def test_report_write_lock_conflict_is_e_lock(default_report):
    engine, report = default_report
    lock = FileLock(engine.lock_path)
    lock.acquire()
    try:
        with pytest.raises(EngineError) as exc_info:
            engine.write_report(report)
        assert exc_info.value.code == "E_LOCK"
    finally:
        lock.release()


def test_checkpoint_and_report_are_parseable(default_report):
    engine, report = default_report
    parsed_report = json.loads(
        engine.report_path.read_text(encoding="utf-8")
    )
    assert parsed_report["status"] == "OK"
    snapshot = (
        engine.checkpoint_dir / report["checkpoint"]["latest_saved"]["snapshot_name"]
    )
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["structure_version"] == 1
    assert payload["applied_batch_ids"] == ["P100", "P150", "P200", "P300"]
    assert payload["state"]["clusters"]
