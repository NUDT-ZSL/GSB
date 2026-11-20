from __future__ import annotations

import json
import subprocess
import sys

from .conftest import ROOT


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "shardpipe", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_cli_status_is_stably_sorted_json(tmp_path, sample_path):
    state_dir = tmp_path / "state"
    loaded = _run(
        "load", "--input", str(sample_path), "--state-dir", str(state_dir)
    )
    assert loaded.returncode == 0, loaded.stderr
    scheduled = _run(
        "schedule",
        "--state-dir",
        str(state_dir),
        "--job",
        "train-alpha",
        "--run-id",
        "cli-run",
    )
    assert scheduled.returncode == 0, scheduled.stderr
    first = _run(
        "status", "--state-dir", str(state_dir), "--run-id", "cli-run"
    )
    second = _run(
        "status", "--state-dir", str(state_dir), "--run-id", "cli-run"
    )
    assert first.returncode == 0
    assert first.stdout == second.stdout
    parsed = json.loads(first.stdout)
    assert first.stdout.strip() == json.dumps(
        parsed, indent=2, sort_keys=True, ensure_ascii=False
    )


def test_cli_verify_and_resume_of_complete_run(tmp_path, sample_path):
    state_dir = tmp_path / "state"
    assert _run(
        "load", "--input", str(sample_path), "--state-dir", str(state_dir)
    ).returncode == 0
    assert _run(
        "schedule",
        "--state-dir",
        str(state_dir),
        "--job",
        "train-alpha",
        "--run-id",
        "verify-run",
    ).returncode == 0
    verified = _run(
        "verify", "--state-dir", str(state_dir), "--run-id", "verify-run"
    )
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["ok"] is True
    resumed = _run(
        "resume", "--state-dir", str(state_dir), "--run-id", "verify-run"
    )
    assert resumed.returncode == 0
    assert json.loads(resumed.stdout)["already_complete"] is True
