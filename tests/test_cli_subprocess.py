"""Subprocess-level CLI tests: graceful signal exit and resume equivalence."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(ROOT / ".venv" / "bin" / "python")
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src")}


def _cli(snapshot_dir: Path, report: Path, baseline: Path, env_extra=None,
         config="configs/config.json"):
    env = dict(ENV)
    if env_extra:
        env.update(env_extra)
    cmd = [
        PYTHON, "-m", "voiceworkbench.cli",
        "--config", config,
        "--audio-dir", "data/audio",
        "--commands", "commands/edits.jsonl",
        "--snapshot-dir", str(snapshot_dir),
        "--report", str(report),
        "--baseline-report", str(baseline),
    ]
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)


def test_full_cli_run_exit_zero(tmp_path):
    proc = _cli(tmp_path / "snap", tmp_path / "report.json",
                tmp_path / "baseline.json")
    assert proc.returncode == 0, proc.stderr
    report = json.loads((tmp_path / "report.json").read_text())
    baseline = json.loads((tmp_path / "baseline.json").read_text())
    assert report["stage_keys"] == baseline["stage_keys"]
    assert report["clusters"] == baseline["clusters"]


def test_sigterm_graceful_exit_then_resume(tmp_path):
    snap_dir = tmp_path / "snap"
    partial_report = tmp_path / "partial.json"
    # slow each budget check so a SIGTERM lands while commands are processing
    env = {"WORKBENCH_SLOW_MS": "5"}
    proc = subprocess.Popen(
        [
            PYTHON, "-m", "voiceworkbench.cli",
            "--config", "configs/config.json",
            "--audio-dir", "data/audio",
            "--commands", "commands/edits.jsonl",
            "--snapshot-dir", str(snap_dir),
            "--report", str(partial_report),
            "--baseline-report", str(tmp_path / "partial_baseline.json"),
        ],
        cwd=ROOT, env={**ENV, **env},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # allow the first snapshot to land, then request graceful termination
    deadline = time.time() + 20
    while time.time() < deadline and not (snap_dir / "snapshot.json").exists():
        time.sleep(0.05)
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("process did not finish the current command snapshot after SIGTERM")
    assert proc.returncode == 0

    # resume in a fresh process and confirm equivalence to an uninterrupted run
    resumed_report = tmp_path / "resumed.json"
    resumed_baseline = tmp_path / "resumed_baseline.json"
    resume = _cli(snap_dir, resumed_report, resumed_baseline)
    assert resume.returncode == 0, resume.stderr
    report = json.loads(resumed_report.read_text())
    baseline = json.loads(resumed_baseline.read_text())
    assert report["stage_keys"] == baseline["stage_keys"]
    assert report["clusters"] == baseline["clusters"]
    assert report["assignments"] == baseline["assignments"]


def test_bad_audio_dir_is_listed_in_failures(tmp_path):
    # point the CLI at the bad-audio directory: format failures are isolated
    cmd = [
        PYTHON, "-m", "voiceworkbench.cli",
        "--config", "configs/config.json",
        "--audio-dir", "data/bad_audio",
        "--commands", "commands/edits.jsonl",
        "--snapshot-dir", str(tmp_path / "snap-bad"),
        "--report", str(tmp_path / "bad-report.json"),
        "--baseline-report", str(tmp_path / "bad-baseline.json"),
    ]
    proc = subprocess.run(cmd, cwd=ROOT, env=ENV, capture_output=True, text=True)
    assert proc.returncode == 0
    report = json.loads((tmp_path / "bad-report.json").read_text())
    codes = {item["code"] for item in report["audio_failures"]}
    assert codes == {"E_AUDIO_FORMAT"}
    assert len(report["audio_failures"]) >= 3
