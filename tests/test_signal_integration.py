from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from relief_workbench.sample import generate_sample

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _wait_for_tile(state_dir: Path, timeout: float = 10.0) -> None:
    tiles_dir = state_dir / "tiles"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tiles_dir.exists() and any(tiles_dir.glob("*.tile")):
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the first completed tile")


def _start_render(config_path: Path, state: Path, output: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "relief_workbench",
            "render",
            "--config",
            str(config_path),
            "--state",
            str(state),
            "--output",
            str(output),
            "--tile-delay-ms",
            "60",
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _restart_and_parse(config_path: Path, state: Path, output: Path) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "relief_workbench",
            "render",
            "--config",
            str(config_path),
            "--state",
            str(state),
            "--output",
            str(output),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    return json.loads(completed.stdout)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal semantics required")
def test_sigterm_and_sigkill_resume(tmp_path) -> None:
    sample = tmp_path / "sample"
    truth = generate_sample(sample)
    config_path = sample / "sample_config.json"

    state = tmp_path / "state-term"
    output = tmp_path / "out-term"
    process = _start_render(config_path, state, output)
    try:
        _wait_for_tile(state)
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    assert process.returncode == 143
    marker = json.loads((state / "interrupted.json").read_text(encoding="utf-8"))
    assert marker["exit_code"] == 143
    assert not (output / "report.json").exists()

    resumed = _restart_and_parse(config_path, state, output)
    assert resumed["ok"] is True
    assert resumed["previous_interruption"] is not None
    assert resumed["comparison"]["max_abs_error"] == 0
    assert resumed["comparison"]["assembled_sha256"] == truth["gray_sha256"]

    kill_state = tmp_path / "state-kill"
    kill_output = tmp_path / "out-kill"
    process = _start_render(config_path, kill_state, kill_output)
    try:
        _wait_for_tile(kill_state)
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    assert process.returncode != 0
    assert not (kill_state / "interrupted.json").exists()

    resumed_after_kill = _restart_and_parse(config_path, kill_state, kill_output)
    assert resumed_after_kill["ok"] is True
    assert resumed_after_kill["previous_interruption"] is None
    assert resumed_after_kill["cache"]["reused_tiles"] >= 1
    assert resumed_after_kill["comparison"]["max_abs_error"] == 0
    assert resumed_after_kill["comparison"]["assembled_sha256"] == truth["gray_sha256"]
