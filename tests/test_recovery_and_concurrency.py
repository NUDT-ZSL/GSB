from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import ROOT, cli_json, load_sample


def _bytes(path: Path) -> bytes:
    return path.read_bytes()


def test_torn_spectrum_recovery_is_byte_identical(tmp_path: Path):
    base = tmp_path / "base.sqlite"
    crash = tmp_path / "crash.sqlite"
    assert load_sample(base, requests="requests_long_backlog.json")[0] == 0
    assert load_sample(crash, requests="requests_long_backlog.json")[0] == 0
    assert cli_json("advance", "--db", str(base), "--steps", "12")[0] == 0

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "batchdiag",
            "advance",
            "--db",
            str(crash),
            "--steps",
            "12",
        ],
        cwd=ROOT,
        env={
            **__import__("os").environ,
            "BATCHDIAG_FAULT": "torn_spectrum",
            "BATCHDIAG_FAULT_STEP": "3",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 138
    code, status = cli_json("status", "--db", str(crash))
    assert code == 0
    assert status["data"]["pending_artifacts"]["noncommitted_steps"] == [3]
    assert status["data"]["pending_artifacts"]["incomplete_spectrum"]
    cumulative_before = status["data"]["cumulative"]

    code, resumed = cli_json("resume", "--db", str(crash), "--steps", "9")
    assert code == 0, resumed
    assert resumed["data"]["recovery"]["repaired_steps"] == [3]
    assert "b000003/memory" in resumed["data"]["recovery"][
        "inserted_spectrum_entries"
    ]
    assert status["data"]["cumulative"] == cumulative_before

    base_report = tmp_path / "base-report.json"
    crash_report = tmp_path / "crash-report.json"
    assert cli_json(
        "spectrum", "--db", str(base), "--output", str(base_report)
    )[0] == 0
    assert cli_json(
        "spectrum", "--db", str(crash), "--output", str(crash_report)
    )[0] == 0
    assert _bytes(base_report) == _bytes(crash_report)
    code, verified = cli_json(
        "verify", "--db", str(crash), "--report", str(base_report)
    )
    assert code == 0, verified


def test_crash_before_and_after_commit_resume_consistently(tmp_path: Path):
    base = tmp_path / "base.sqlite"
    assert load_sample(base, requests="requests_long_backlog.json")[0] == 0
    assert cli_json("advance", "--db", str(base), "--steps", "10")[0] == 0
    base_report = tmp_path / "base.json"
    assert cli_json(
        "spectrum", "--db", str(base), "--output", str(base_report)
    )[0] == 0

    for fault, resume_steps in (
        ("kill_before_commit", 8),
        ("kill_after_commit", 7),
    ):
        db = tmp_path / f"{fault}.sqlite"
        assert load_sample(db, requests="requests_long_backlog.json")[0] == 0
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "batchdiag",
                "advance",
                "--db",
                str(db),
                "--steps",
                "10",
            ],
            cwd=ROOT,
            env={
                **__import__("os").environ,
                "BATCHDIAG_FAULT": fault,
                "BATCHDIAG_FAULT_STEP": "3",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        assert process.returncode == 137
        code, resumed = cli_json("resume", "--db", str(db), "--steps", str(resume_steps))
        assert code == 0, resumed
        report_path = tmp_path / f"{fault}.json"
        assert cli_json(
            "spectrum", "--db", str(db), "--output", str(report_path)
        )[0] == 0
        assert _bytes(report_path) == _bytes(base_report)


def test_resume_without_range_advances_to_final_outcomes(tmp_path: Path):
    crash = tmp_path / "auto-crash.sqlite"
    baseline = tmp_path / "auto-baseline.sqlite"
    assert load_sample(crash, requests="requests_long_backlog.json")[0] == 0
    assert load_sample(baseline, requests="requests_long_backlog.json")[0] == 0
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "batchdiag",
            "advance",
            "--db",
            str(crash),
            "--steps",
            "12",
        ],
        cwd=ROOT,
        env={
            **__import__("os").environ,
            "BATCHDIAG_FAULT": "torn_spectrum",
            "BATCHDIAG_FAULT_STEP": "3",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert process.returncode == 138
    code, resumed = cli_json("resume", "--db", str(crash))
    assert code == 0, resumed
    code, status = cli_json("status", "--db", str(crash))
    assert code == 0
    assert status["data"]["request_status_counts"] == {"completed": 40}
    assert status["data"]["queue_count"] == 0
    assert status["data"]["step_index"] == 10

    assert cli_json("advance", "--db", str(baseline), "--steps", "10")[0] == 0
    crash_report = tmp_path / "auto-crash.json"
    baseline_report = tmp_path / "auto-baseline.json"
    assert cli_json(
        "spectrum", "--db", str(crash), "--output", str(crash_report)
    )[0] == 0
    assert cli_json(
        "spectrum", "--db", str(baseline), "--output", str(baseline_report)
    )[0] == 0
    assert _bytes(crash_report) == _bytes(baseline_report)


def test_concurrent_advances_do_not_duplicate_work(tmp_path: Path):
    concurrent = tmp_path / "concurrent.sqlite"
    baseline = tmp_path / "baseline.sqlite"
    assert load_sample(concurrent, requests="requests_long_backlog.json")[0] == 0
    assert load_sample(baseline, requests="requests_long_backlog.json")[0] == 0
    assert cli_json("advance", "--db", str(baseline), "--steps", "20")[0] == 0

    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "batchdiag",
                "advance",
                "--db",
                str(concurrent),
                "--steps",
                "5",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    outputs = [process.communicate(timeout=60) for process in processes]
    for process, (stdout, stderr) in zip(processes, outputs):
        assert process.returncode == 0, (stdout, stderr)
        assert json.loads(stdout)["ok"] is True

    code, status = cli_json("status", "--db", str(concurrent))
    assert code == 0
    assert status["data"]["step_index"] == 20
    assert status["data"]["batch_count"] == 10
    code, verified = cli_json("verify", "--db", str(concurrent))
    assert code == 0, verified

    baseline_report = tmp_path / "baseline-report.json"
    concurrent_report = tmp_path / "concurrent-report.json"
    assert cli_json(
        "spectrum", "--db", str(baseline), "--output", str(baseline_report)
    )[0] == 0
    assert cli_json(
        "spectrum", "--db", str(concurrent), "--output", str(concurrent_report)
    )[0] == 0
    assert _bytes(baseline_report) == _bytes(concurrent_report)
