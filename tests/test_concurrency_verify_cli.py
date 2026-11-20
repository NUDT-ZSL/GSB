from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from conftest import (
    REPO_ROOT,
    copy_samples,
    invoke,
    payload,
    read_manifest,
    run_sample,
    write_manifest,
)


def test_two_fresh_runs_are_byte_repeatable(tmp_path: Path) -> None:
    first_workspace = copy_samples(tmp_path / "one")
    second_workspace = copy_samples(tmp_path / "two")
    first = run_sample(first_workspace)
    second = run_sample(second_workspace)
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    expected = json.loads(
        (REPO_ROOT / "samples/expected_summary.json").read_text(encoding="utf-8")
    )
    summary = payload(first)
    combined = next(task for task in summary["tasks"] if task["task"] == "combined")
    assert combined["output_digest"] == expected["tasks"]["combined"]["output_sha256"]
    assert summary["logical_clock_watermark"] == expected["logical_clock_watermark"]
    for workspace in (first_workspace, second_workspace):
        verified = invoke(
            workspace,
            "verify",
            "--manifest",
            "samples/manifest.json",
            "--state",
            "var/state.sqlite3",
            "--cache",
            "var/cache",
        )
        assert verified.returncode == 0
        assert payload(verified)["valid"] is True


def test_clock_regression_and_tamper_detection(sample_workspace: Path) -> None:
    assert run_sample(sample_workspace).returncode == 0
    regression = invoke(
        sample_workspace,
        "resume",
        "--manifest",
        "samples/manifest.json",
        "--state",
        "var/state.sqlite3",
        "--cache",
        "var/cache",
        "--clock-start-ms",
        "0",
    )
    assert regression.returncode == 7
    assert payload(regression)["error"]["code"] == "E_CLOCK_REGRESSION"

    target = sample_workspace / "artifacts/demo/results/combined.json"
    target.write_bytes(target.read_bytes() + b"tampered\n")
    verify = invoke(
        sample_workspace,
        "verify",
        "--manifest",
        "samples/manifest.json",
        "--state",
        "var/state.sqlite3",
        "--cache",
        "var/cache",
    )
    assert verify.returncode == 6
    result = payload(verify)
    assert result["valid"] is False
    assert any(error["code"] == "E_DIGEST_MISMATCH" for error in result["errors"])

    repaired = run_sample(sample_workspace)
    assert repaired.returncode == 0
    verified = invoke(
        sample_workspace,
        "verify",
        "--manifest",
        "samples/manifest.json",
        "--state",
        "var/state.sqlite3",
        "--cache",
        "var/cache",
    )
    assert verified.returncode == 0


def test_concurrent_instances_commit_each_cache_key_once(sample_workspace: Path) -> None:
    command = [
        sys.executable,
        str(REPO_ROOT / "pipeline.py"),
        "run",
        "--manifest",
        "samples/manifest.json",
        "--state",
        "var/shared/state.sqlite3",
        "--cache",
        "var/shared/cache",
        "--workspace",
        str(sample_workspace),
    ]
    environment = os.environ.copy()
    first = subprocess.Popen(
        command,
        cwd=sample_workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    second = subprocess.Popen(
        command,
        cwd=sample_workspace,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    first_out, first_err = first.communicate(timeout=60)
    second_out, second_err = second.communicate(timeout=60)
    assert first.returncode == 0, first_out + first_err
    assert second.returncode == 0, second_out + second_err

    verified = invoke(
        sample_workspace,
        "verify",
        "--manifest",
        "samples/manifest.json",
        "--state",
        "var/shared/state.sqlite3",
        "--cache",
        "var/shared/cache",
    )
    assert verified.returncode == 0
    with sqlite3.connect(sample_workspace / "var/shared/state.sqlite3") as connection:
        duplicates = connection.execute(
            "SELECT task_name, COUNT(*) FROM cache_entries "
            "GROUP BY task_name HAVING COUNT(*) > 1"
        ).fetchall()
        commits = connection.execute(
            "SELECT COUNT(*) FROM task_states WHERE phase='committed'"
        ).fetchone()[0]
    assert duplicates == []
    assert commits == 5

