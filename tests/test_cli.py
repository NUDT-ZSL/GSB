from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from slpguard.io_utils import read_json_envelope


ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "slpguard", *args],
        cwd=str(cwd or ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_valid_file_exit_zero(corpus, tmp_path: Path) -> None:
    report_path = tmp_path / "valid.json"
    completed = _run(
        str(corpus / "valid_slp.slp"),
        "--no-checkpoint",
        "--no-clean-temp",
        "--report",
        str(report_path),
    )
    assert completed.returncode == 0, completed.stderr
    stdout = json.loads(completed.stdout)
    assert stdout["batch"]["success"] == 1
    payload = read_json_envelope(report_path, kind="slp-report")
    assert payload["files"][0]["status"] == "ok"
    assert payload["files"][0]["truth"]["all_consistent"] is True


def test_cli_invalid_sample_exit_one(corpus, tmp_path: Path) -> None:
    report_path = tmp_path / "invalid.json"
    completed = _run(
        str(corpus / "half_close_violation.slp"),
        "--no-checkpoint",
        "--no-clean-temp",
        "--report",
        str(report_path),
    )
    assert completed.returncode == 1
    payload = read_json_envelope(report_path, kind="slp-report")
    codes = {
        violation["code"]
        for item in payload["files"]
        for session in item["sessions"]
        for violation in session["violations"]
    }
    assert codes == {"E_AFTER_CLOSE"}


def test_cli_directory_mode_and_timeout(corpus, tmp_path: Path) -> None:
    report_path = tmp_path / "dir.json"
    completed = _run(
        str(corpus),
        "--workers",
        "6",
        "--max-seconds",
        "0",
        "--no-checkpoint",
        "--no-clean-temp",
        "--report",
        str(report_path),
    )
    assert completed.returncode == 1
    payload = read_json_envelope(report_path, kind="slp-report")
    assert payload["batch"]["files"] == 21
    assert payload["batch"]["limited"] == 21
    assert all(item["termination"] == "E_TIMEOUT" for item in payload["files"])


def test_cli_argument_error_exit_two() -> None:
    completed = _run("does-not-exist.slp")
    assert completed.returncode == 2
    assert "does not exist" in completed.stderr
