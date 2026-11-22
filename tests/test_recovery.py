from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import ROOT, cli
from fieldengine import engine


def test_sigkill_during_execution_then_resume(tmp_path: Path) -> None:
    db = tmp_path / "crash_running.sqlite3"
    experiment = ROOT / "samples/crash_running/experiment.json"
    first = cli(
        "run",
        "--experiment",
        str(experiment),
        "--db",
        str(db),
        cwd=tmp_path,
    )
    assert first.returncode in {-9, 137}
    before = engine.status(db)
    counts = before["shard_watermark"]["counts"]
    assert counts.get("COMMITTED", 0) >= 1
    interrupted = [
        item
        for item in before["shard_watermark"]["shards"]
        if item["status"] == "RUNNING"
    ]
    assert len(interrupted) == 1
    assert interrupted[0]["lease"]["owner_alive"] is False
    resumed = engine.resume(experiment, db)
    assert resumed["ok"] is True
    assert resumed["shard_counts"] == {"COMMITTED": 6}
    verified = engine.verify(experiment, db)
    assert verified["ok"] is True
    after = engine.status(db)
    assert all(item["status"] == "COMMITTED" for item in after["shard_watermark"]["shards"])
    assert sum(item["interrupted_attempts"] for item in after["shard_watermark"]["shards"]) == 1
    assert after["run"]["logical_clock"] > before["run"]["logical_clock"]


def test_sigkill_after_commit_then_resume_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "crash_commit.sqlite3"
    experiment = ROOT / "samples/crash/experiment.json"
    first = cli(
        "run",
        "--experiment",
        str(experiment),
        "--db",
        str(db),
        cwd=tmp_path,
    )
    assert first.returncode in {-9, 137}
    before = engine.status(db)
    assert before["shard_watermark"]["counts"].get("COMMITTED", 0) >= 2
    resumed = engine.resume(experiment, db)
    assert resumed["ok"] is True
    verified = engine.verify(experiment, db)
    assert verified["ok"] is True
    second_resume = engine.resume(experiment, db)
    assert second_resume["ok"] is True
    assert second_resume["run"]["logical_clock"] == resumed["run"]["logical_clock"]
    rerun = engine.run(experiment, db)
    assert rerun["ok"] is True
    assert rerun["run"]["logical_clock"] == resumed["run"]["logical_clock"]


def test_completed_run_duplicate_shard_submission_is_retained_once(tmp_path: Path) -> None:
    from fieldengine.model import load_experiment
    from fieldengine.planning import build_plan
    from fieldengine.store import Store

    experiment = load_experiment(ROOT / "samples/basic/experiment.json")
    plan = build_plan(experiment)
    db = tmp_path / "idempotent.sqlite3"
    assert engine.run(experiment.experiment_path, db)["ok"] is True
    store = Store(db)
    groups, windows = store.read_results()
    assert len(groups) == 4
    assert len(windows) == 8
    store.complete_run(experiment, groups, windows)
    assert store.run_summary()["run"]["status"] == "COMPLETE"
    store.close()


def test_duplicate_shard_commit_is_idempotent(tmp_path: Path) -> None:
    from fieldengine.execution import execute_shard
    from fieldengine.model import load_experiment
    from fieldengine.planning import build_plan
    from fieldengine.store import Store

    experiment = load_experiment(ROOT / "samples/basic/experiment.json")
    plan = build_plan(experiment)
    db = tmp_path / "duplicate.sqlite3"
    store = Store(db)
    store.initialize(experiment, plan)
    claimed = store.claim_shards(experiment, plan, limit=1)
    assert len(claimed) == 1
    partial = execute_shard(experiment, plan, claimed[0])
    assert store.commit_shard(experiment, partial) == []
    clock_after_first = store.run_summary()["run"]["logical_clock"]
    assert store.commit_shard(experiment, partial) == []
    assert store.run_summary()["run"]["logical_clock"] == clock_after_first
    status = store.status()
    assert status["shard_watermark"]["counts"] == {"COMMITTED": 1, "PENDING": 11}
    store.close()


def test_two_processes_resume_same_state_concurrently(tmp_path: Path) -> None:
    db = tmp_path / "concurrent.sqlite3"
    experiment = ROOT / "samples/crash_running/experiment.json"
    crashed = cli(
        "run",
        "--experiment",
        str(experiment),
        "--db",
        str(db),
        cwd=tmp_path,
    )
    assert crashed.returncode in {-9, 137}
    command = [
        sys.executable,
        "-m",
        "fieldengine",
        "resume",
        "--experiment",
        str(experiment),
        "--db",
        str(db),
    ]
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    first = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    second = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    first_out, first_err = first.communicate(timeout=30)
    second_out, second_err = second.communicate(timeout=30)
    assert first.returncode == 0, (first_out, first_err)
    assert second.returncode == 0, (second_out, second_err)
    assert json.loads(first_out)["ok"] is True
    assert json.loads(second_out)["ok"] is True
    final_status = engine.status(db)
    assert final_status["shard_watermark"]["counts"] == {"COMMITTED": 6}
    assert final_status["budget"]["active_leases"] == 0
    assert engine.verify(experiment, db)["ok"] is True
