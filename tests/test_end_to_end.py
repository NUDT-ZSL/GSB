from __future__ import annotations

import json
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from latent_diag.executor import run_engine
from latent_diag.selfcheck import run_selfcheck
from latent_diag.snapshots import SnapshotStore


ROOT = Path(__file__).resolve().parents[1]


def _run(
    sample_dir: Path,
    state: Path,
    tasks: str = "tasks.jsonl",
    *,
    workers: int | None = None,
    **kwargs,
):
    return run_engine(
        tasks_path=sample_dir / tasks,
        config_path=sample_dir / "config.json",
        db_path=state / "ledger.sqlite",
        state_dir=state,
        worker_count=workers,
        **kwargs,
    )


def test_cache_revision_propagation_and_selfcheck(sample_dir, sample_config, tmp_path):
    state = tmp_path / "state"
    first = _run(sample_dir, state, workers=2, tasks_per_batch=2)
    assert first["status"] == "ok"
    assert len(first["results"]["recomputed"]) == 6
    assert first["results"]["reused"] == []

    second = _run(sample_dir, state, workers=2, tasks_per_batch=2)
    assert second["status"] == "ok"
    assert set(second["results"]["reused"]) == {
        "t001_forest_f64",
        "t002_city_f32",
        "t003_ocean_fixed",
        "t004_robot_f64",
        "t005_desert_f32",
        "t006_snow_fixed",
    }
    assert second["results"]["recomputed"] == []

    changed = _run(
        sample_dir,
        state,
        tasks="tasks_v2_dtype_change.jsonl",
        workers=2,
        tasks_per_batch=2,
    )
    assert changed["status"] == "ok"
    assert changed["invalidation"]["affected"] == ["t002_city_f32", "t004_robot_f64"]
    assert changed["results"]["recomputed"] == ["t002_city_f32", "t004_robot_f64"]
    assert set(changed["results"]["reused"]) == {
        "t001_forest_f64",
        "t003_ocean_fixed",
        "t005_desert_f32",
        "t006_snow_fixed",
    }
    assert changed["results"]["reuse_recompute_disjoint"] is True
    assert changed["snapshots"]["bundle_version"] == 3
    assert set(changed["snapshots"]["versions"].values()) == {3}

    check = run_selfcheck(
        db_path=state / "ledger.sqlite",
        state_dir=state,
        config=sample_config,
    )
    assert check["ok"], [item for item in check["checks"] if not item["ok"]]


def test_worker_and_batch_configuration_do_not_change_ledger(sample_dir, tmp_path):
    serial_state = tmp_path / "serial"
    parallel_state = tmp_path / "parallel"
    serial = _run(sample_dir, serial_state, workers=1, tasks_per_batch=1)
    parallel = _run(sample_dir, parallel_state, workers=3, tasks_per_batch=4)
    assert serial["status"] == "ok"
    assert parallel["status"] == "ok"
    serial_con = sqlite3.connect(serial_state / "ledger.sqlite")
    serial_con.row_factory = sqlite3.Row
    serial_hashes = {
        row["task_id"]: row["content_hash"]
        for row in serial_con.execute(
            "SELECT task_id, content_hash FROM ledger_entries"
        )
    }
    parallel_con = sqlite3.connect(parallel_state / "ledger.sqlite")
    parallel_con.row_factory = sqlite3.Row
    parallel_hashes = {
        row["task_id"]: row["content_hash"]
        for row in parallel_con.execute(
            "SELECT task_id, content_hash FROM ledger_entries"
        )
    }
    assert serial_hashes == parallel_hashes
    assert serial["schedule"]["dispatch_order"] == parallel["schedule"]["dispatch_order"]
    assert serial["schedule"]["merge_order"] == parallel["schedule"]["merge_order"]


def test_removed_manifest_tasks_are_retired_from_current_ledger(
    sample_dir, sample_config, tmp_path
):
    from latent_diag.task import load_manifest

    state = tmp_path / "state"
    first = _run(sample_dir, state, workers=2)
    assert first["status"] == "ok"
    tasks = load_manifest(sample_dir / "tasks.jsonl")
    subset = tmp_path / "tasks_subset.jsonl"
    subset.write_text(
        "".join(
            json.dumps(task.storage_dict(), ensure_ascii=False, sort_keys=True) + "\n"
            for task in tasks[:-1]
        ),
        encoding="utf-8",
    )
    summary = run_engine(
        tasks_path=subset,
        config_path=sample_dir / "config.json",
        db_path=state / "ledger.sqlite",
        state_dir=state,
        worker_count=2,
        tasks_per_batch=2,
    )
    assert summary["status"] == "ok"
    assert summary["invalidation"]["removed"] == [tasks[-1].task_id]
    con = sqlite3.connect(state / "ledger.sqlite")
    assert con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 5
    assert con.execute(
        "SELECT COUNT(*) FROM ledger_entries WHERE task_id = ?",
        (tasks[-1].task_id,),
    ).fetchone()[0] == 0
    check = run_selfcheck(
        db_path=state / "ledger.sqlite",
        state_dir=state,
        config=sample_config,
    )
    assert check["ok"], [item for item in check["checks"] if not item["ok"]]


def test_tolerance_error_contains_task_and_first_position(sample_dir, tmp_path):
    state = tmp_path / "state"
    summary = run_engine(
        tasks_path=sample_dir / "tasks.jsonl",
        config_path=sample_dir / "config_tolerance_tight.json",
        db_path=state / "ledger.sqlite",
        state_dir=state,
        worker_count=2,
    )
    assert summary["status"] == "tolerance_failed"
    assert "E_TOLERANCE_EXCEEDED" in summary["error_codes"]
    assert summary["results"]["tolerance_failures"]
    for failure in summary["results"]["tolerance_failures"]:
        assert failure["code"] == "E_TOLERANCE_EXCEEDED"
        assert failure["task_id"]
        assert failure["first_deviation"] is not None
        assert len(failure["first_deviation"]) == 3

    con = sqlite3.connect(state / "ledger.sqlite")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT task_id,error_code,error_json FROM tasks WHERE error_code IS NOT NULL"
    ).fetchall()
    assert rows
    for row in rows:
        error = json.loads(row["error_json"])
        assert error["code"] == "E_TOLERANCE_EXCEEDED"
        assert error["task_id"] == row["task_id"]
        assert len(error["first_deviation"]) == 3


def test_stage_busy_does_not_cancel_task_boundaries(sample_dir, tmp_path):
    state = tmp_path / "state"
    first = _run(sample_dir, state, workers=2)
    assert first["status"] == "ok"
    previous_snapshot = json.loads(
        (state / "snapshots" / "CURRENT.json").read_text(encoding="utf-8")
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "latent_diag",
            "snapshot-hold",
            "--state",
            str(state),
            "--seconds",
            "3",
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        line = holder.stdout.readline()
        assert json.loads(line)["event"] == "lock_held"
        with pytest.raises(Exception) as caught:
            run_engine(
                tasks_path=sample_dir / "tasks.jsonl",
                config_path=sample_dir / "config.json",
                db_path=state / "ledger.sqlite",
                state_dir=state,
                worker_count=2,
                tasks_per_batch=2,
                recompute_committed=True,
            )
        assert getattr(caught.value, "code", None) == "E_STAGE_BUSY"
    finally:
        holder.wait(timeout=10)

    unchanged = json.loads(
        (state / "snapshots" / "CURRENT.json").read_text(encoding="utf-8")
    )
    assert unchanged["bundle_version"] == previous_snapshot["bundle_version"]
    con = sqlite3.connect(state / "ledger.sqlite")
    task_count, ok_count = con.execute(
        "SELECT COUNT(*), SUM(status='ok') FROM tasks"
    ).fetchone()
    assert task_count == 6
    assert ok_count == 6
    assert con.execute(
        "SELECT status FROM runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()[0] == "stage_busy"


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "latent_diag", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def test_forced_termination_resume_and_committed_recompute(sample_dir, sample_config, tmp_path):
    state = tmp_path / "state"
    crashed = _cli(
        "run",
        "--tasks",
        str(sample_dir / "tasks.jsonl"),
        "--config",
        str(sample_dir / "config.json"),
        "--db",
        str(state / "ledger.sqlite"),
        "--state",
        str(state),
        "--workers",
        "2",
        "--batch-size",
        "2",
        "--crash-after-checkpoints",
        "2",
    )
    assert crashed.returncode == -signal.SIGKILL or crashed.returncode == 137
    con = sqlite3.connect(state / "ledger.sqlite")
    assert con.execute("SELECT COUNT(*) FROM checkpoint_entries").fetchone()[0] == 2
    assert not (state / "snapshots" / "CURRENT.json").exists()

    resumed = _run(sample_dir, state, workers=2, tasks_per_batch=2)
    assert resumed["status"] == "ok"
    assert resumed["results"]["reused"] == ["t001_forest_f64", "t002_city_f32"]
    assert len(resumed["results"]["recomputed"]) == 4
    assert resumed["results"]["reuse_recompute_disjoint"] is True

    check = run_selfcheck(
        db_path=state / "ledger.sqlite",
        state_dir=state,
        config=sample_config,
    )
    assert check["ok"], [item for item in check["checks"] if not item["ok"]]

    recomputed = _run(
        sample_dir,
        state,
        workers=2,
        tasks_per_batch=2,
        recompute_committed=True,
    )
    assert recomputed["status"] == "ok"
    assert recomputed["results"]["reused"] == []
    assert len(recomputed["results"]["recomputed"]) == 6
    check_after = run_selfcheck(
        db_path=state / "ledger.sqlite",
        state_dir=state,
        config=sample_config,
    )
    assert check_after["ok"]
    verified = next(
        item
        for item in check_after["checks"]
        if item["name"] == "recompute_previous_ledger_match"
    )
    assert verified["ok"]
    assert all(event["matches_previous"] for event in verified["events"])

