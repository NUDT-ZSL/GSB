#!/usr/bin/env python3
from __future__ import annotations

import json
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from h2diag.canonical import pretty_json
from h2diag.sample import write_sample_scenario


RUNS = ROOT / "artifacts" / "runs"


def run_cli(args: list[str], expected: int) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "h2diag", *args]
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != expected:
        raise RuntimeError(
            f"command returned {completed.returncode}, expected {expected}: "
            f"{' '.join(command)}\nstdout={completed.stdout}\n"
            f"stderr={completed.stderr}"
        )
    return completed


def main() -> int:
    RUNS.mkdir(parents=True, exist_ok=True)
    scenario_path = ROOT / "samples" / "demo_scenario.json"
    write_sample_scenario(scenario_path)

    clean_db = RUNS / "demo_clean.db"
    crash_db = RUNS / "demo_crash.db"
    clean_trace = RUNS / "demo_clean.trace.jsonl"
    crash_trace = RUNS / "demo_crash.trace.jsonl"
    clean_status_path = RUNS / "demo_final_status.json"
    before_status_path = RUNS / "demo_before_resume_status.json"
    report_path = RUNS / "validation_report.json"

    for path in (
        clean_db,
        crash_db,
        Path(str(clean_db) + "-wal"),
        Path(str(clean_db) + "-shm"),
        Path(str(crash_db) + "-wal"),
        Path(str(crash_db) + "-shm"),
        clean_trace,
        crash_trace,
        clean_status_path,
        before_status_path,
        report_path,
    ):
        if path.exists():
            path.unlink()

    clean_run = run_cli(
        [
            "run",
            "--scenario",
            str(scenario_path),
            "--db",
            str(clean_db),
            "--run-id",
            "demo",
            "--trace",
            str(clean_trace),
        ],
        expected=0,
    )
    clean_verify = run_cli(
        [
            "verify",
            "--scenario",
            str(scenario_path),
            "--db",
            str(clean_db),
            "--run-id",
            "demo",
        ],
        expected=0,
    )

    crash_run = run_cli(
        [
            "run",
            "--scenario",
            str(scenario_path),
            "--db",
            str(crash_db),
            "--run-id",
            "demo",
            "--trace",
            str(crash_trace),
            "--crash-before-commit",
            "15",
        ],
        expected=91,
    )
    before_resume = run_cli(
        [
            "status",
            "--db",
            str(crash_db),
            "--run-id",
            "demo",
        ],
        expected=0,
    )
    before_status = json.loads(before_resume.stdout)
    if before_status["run"]["status"] != "running":
        raise RuntimeError("crashed run must still be marked running")
    if before_status["run"]["committed_seq"] != 14:
        raise RuntimeError("half-committed frame 15 became visible")

    resumed_run = run_cli(
        [
            "resume",
            "--scenario",
            str(scenario_path),
            "--db",
            str(crash_db),
            "--run-id",
            "demo",
            "--trace",
            str(crash_trace),
        ],
        expected=0,
    )
    resumed_verify = run_cli(
        [
            "verify",
            "--scenario",
            str(scenario_path),
            "--db",
            str(crash_db),
            "--run-id",
            "demo",
        ],
        expected=0,
    )

    clean_status = json.loads(clean_run.stdout)
    resumed_status = json.loads(resumed_run.stdout)
    clean_report = json.loads(clean_verify.stdout)
    resumed_report = json.loads(resumed_verify.stdout)
    comparison = {
        "final_ledger_digest_identical": (
            clean_status["run"]["ledger_digest"]
            == resumed_status["run"]["ledger_digest"]
        ),
        "final_state_digest_identical": (
            clean_status["run"]["state_digest"]
            == resumed_status["run"]["state_digest"]
        ),
        "final_trajectory_digest_identical": (
            clean_status["run"]["trajectory_digest"]
            == resumed_status["run"]["trajectory_digest"]
        ),
        "frame_trace_byte_identical": (
            clean_trace.read_bytes() == crash_trace.read_bytes()
        ),
        "status_json_byte_identical": (
            clean_run.stdout.encode("utf-8") == resumed_run.stdout.encode("utf-8")
        ),
    }
    clean_status_path.write_text(
        clean_run.stdout, encoding="utf-8", newline="\n"
    )
    before_status_path.write_text(
        before_resume.stdout, encoding="utf-8", newline="\n"
    )
    report: dict[str, Any] = {
        "clean_run": {
            "committed_seq": clean_status["run"]["committed_seq"],
            "final_ledger_digest": clean_status["run"]["ledger_digest"],
            "final_state_digest": clean_status["run"]["state_digest"],
            "final_trajectory_digest": clean_status["run"]["trajectory_digest"],
            "trace_lines": len(clean_trace.read_text(encoding="utf-8").splitlines()),
            "verify": clean_report,
        },
        "comparison": comparison,
        "environment": {
            "implementation": platform.python_implementation(),
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "system": platform.system(),
        },
        "interrupted_run": {
            "committed_seq_before_resume": before_status["run"]["committed_seq"],
            "forced_exit_code": crash_run.returncode,
            "half_committed_frame": 15,
            "status_before_resume": before_status["run"]["status"],
            "stderr": crash_run.stderr,
        },
        "ok": all(comparison.values()),
        "resumed_run": {
            "committed_seq": resumed_status["run"]["committed_seq"],
            "trace_lines": len(crash_trace.read_text(encoding="utf-8").splitlines()),
            "verify": resumed_report,
        },
    }
    report_path.write_text(pretty_json(report) + "\n", encoding="utf-8", newline="\n")
    print(pretty_json(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

