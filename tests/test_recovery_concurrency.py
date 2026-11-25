from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from diagnostic_engine.cache import CommitLock
from diagnostic_engine.engine import DiagnosticEngine
from diagnostic_engine.errors import DiagnosticError
from diagnostic_engine.state import StateManager

ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str, workdir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "diagnostic_engine.cli", *args],
        cwd=workdir,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )


def test_force_kill_recovery_matches_uninterrupted_run(fixture_dir: Path, tmp_path: Path):
    killed_work = tmp_path / "killed"
    golden_work = tmp_path / "golden"
    killed_work.mkdir()
    golden_work.mkdir()
    config = fixture_dir / "scenario.json"
    observations = fixture_dir / "observations.jsonl"

    killed = _run_cli(
        "run",
        "--config",
        str(config),
        "--observations",
        str(observations),
        "--workdir",
        str(killed_work),
        "--sigkill-after-commit",
        "3",
        workdir=ROOT,
    )
    assert killed.returncode == -9

    golden = _run_cli(
        "run",
        "--config",
        str(config),
        "--observations",
        str(observations),
        "--workdir",
        str(golden_work),
        workdir=ROOT,
    )
    assert golden.returncode == 0, golden.stderr
    golden_report = json.loads(golden.stdout)

    recovered = _run_cli(
        "run",
        "--config",
        str(config),
        "--observations",
        str(observations),
        "--workdir",
        str(killed_work),
        workdir=ROOT,
    )
    assert recovered.returncode == 0, recovered.stderr
    recovered_report = json.loads(recovered.stdout)

    assert recovered_report["windows"] == golden_report["windows"]
    recovery_actions = recovered_report["actions"]
    first_window_actions = [a for a in recovery_actions if a["window_index"] == 0]
    assert {a["node"]: a["action"] for a in first_window_actions} == {
        "preprocess": "reused",
        "cross_expand": "reused",
        "standardize": "reused",
        "reduce": "reused",
        "predict": "recomputed",
        "error": "recomputed",
    }

    verify = _run_cli(
        "verify",
        "1",
        "--config",
        str(config),
        "--observations",
        str(observations),
        "--workdir",
        str(killed_work),
        workdir=ROOT,
    )
    assert verify.returncode == 0, verify.stderr
    assert json.loads(verify.stdout)["ok"] is True


def test_uncommitted_orphan_cache_is_removed_on_recovery(config, records, workdir: Path):
    engine = DiagnosticEngine(config, records[:5], workdir=workdir)
    orphan = engine.cache.put(
        "orphan-key",
        "preprocess",
        {"value": 1},
        {"config_version": config["config_version"], "precision_profile": "float64", "window_id": "w"},
    )
    orphan_path = Path(orphan["path"])
    assert orphan_path.exists()
    engine.recover()
    assert not orphan_path.exists()


def test_concurrent_commit_returns_stage_busy_immediately(tmp_path: Path):
    state = StateManager(tmp_path / ".diagnostic" / "state")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    holder = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "diagnostic_engine.cli",
            "hold-commit-lock",
            "--workdir",
            str(tmp_path),
            "--seconds",
            "10",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = holder.stdout.readline().strip()
        assert ready == "LOCK_READY"
        started = time.monotonic()
        with pytest.raises(DiagnosticError) as exc:
            state.commit_snapshot(
                {
                    "window_id": "busy-window",
                    "precision_profile": "float64",
                    "stage_index": 1,
                    "completed_v_idx": [1],
                }
            )
        elapsed = time.monotonic() - started
        assert exc.value.code == "E_STAGE_BUSY"
        assert elapsed < 1.0
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_catchable_signal_writes_exit_report(tmp_path: Path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "diagnostic_engine.cli",
            "wait-signal",
            "--workdir",
            str(tmp_path),
            "--seconds",
            "30",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
    )
    try:
        assert process.stdout.readline().strip() == "WAIT_READY"
        process.send_signal(subprocess.signal.SIGTERM)
        stdout, _ = process.communicate(timeout=5)
        assert "E_SIGNAL_EXIT" in stdout
        report = tmp_path / ".diagnostic" / "state" / "exit_report.json"
        assert report.exists()
        payload = json.loads(report.read_text())
        assert payload["exit_reason"] == "catchable_signal"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
