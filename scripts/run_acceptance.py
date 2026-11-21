#!/usr/bin/env python3
"""Run the complete local acceptance experiment and write a JSON report."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
ACCEPTANCE = ARTIFACTS / "acceptance"
SAMPLES = ROOT / "samples"
SCENARIO = SAMPLES / "scenario.json"


def run_ledger(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "lockledger",
            "--workspace",
            str(ROOT),
            *args,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )


def clipped(text: str, limit: int = 12000) -> str:
    return text if len(text) <= limit else text[-limit:]


def wait_signature(status: dict[str, Any]) -> list[Any]:
    return [
        (
            tx["tx_key"],
            tx["status"],
            [
                (
                    wait["registration_seq"],
                    wait["raw_wait_ms"],
                    wait["fixed_units"],
                    wait["exact_units"],
                    wait["discarded_remainder"],
                )
                for wait in tx["wait_records"]
            ],
        )
        for tx in status["transactions"]
    ]


def main() -> int:
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required")
    if ACCEPTANCE.exists():
        shutil.rmtree(ACCEPTANCE)
    ACCEPTANCE.mkdir(parents=True)

    generated = run_ledger("generate-samples", "--out", str(SAMPLES))
    if generated.returncode != 0:
        raise SystemExit(generated.stdout + generated.stderr)

    tests = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-v"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )
    (ARTIFACTS / "test-output.txt").write_text(
        tests.stdout + tests.stderr, encoding="utf-8"
    )
    if tests.returncode != 0:
        raise SystemExit("test suite failed; see artifacts/test-output.txt")

    clean_dir = ACCEPTANCE / "clean"
    clean_run = run_ledger(
        "run",
        "--sample",
        str(SCENARIO),
        "--workdir",
        str(clean_dir),
    )
    if clean_run.returncode != 0:
        raise SystemExit(clean_run.stdout + clean_run.stderr)
    clean = json.loads(clean_run.stdout)
    (ACCEPTANCE / "status-clean.json").write_text(
        json.dumps(clean, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    workers_dir = ACCEPTANCE / "workers"
    worker_run = run_ledger(
        "run",
        "--sample",
        str(SCENARIO),
        "--workdir",
        str(workers_dir),
        "--workers",
        "2",
    )
    if worker_run.returncode != 0:
        raise SystemExit(worker_run.stdout + worker_run.stderr)
    workers = json.loads(worker_run.stdout)

    crash_dir = ACCEPTANCE / "crash"
    crashed = run_ledger(
        "run",
        "--sample",
        str(SCENARIO),
        "--workdir",
        str(crash_dir),
        "--crash-at",
        "enqueue",
        "--crash-tick",
        "1",
    )
    if crashed.returncode != 137:
        raise SystemExit(
            f"crash injection expected 137, got {crashed.returncode}: "
            f"{crashed.stdout}{crashed.stderr}"
        )
    before_resume = run_ledger("status", "--workdir", str(crash_dir))
    if before_resume.returncode != 0:
        raise SystemExit(before_resume.stdout + before_resume.stderr)
    before = json.loads(before_resume.stdout)
    resumed = run_ledger("resume", "--workdir", str(crash_dir))
    if resumed.returncode != 0:
        raise SystemExit(resumed.stdout + resumed.stderr)
    resumed_status = json.loads(resumed.stdout)
    verify = run_ledger("verify", "--workdir", str(crash_dir))
    if verify.returncode != 0:
        raise SystemExit(verify.stdout + verify.stderr)
    verify_status = json.loads(verify.stdout)
    (ACCEPTANCE / "status-resumed.json").write_text(
        json.dumps(resumed_status, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    deadlock = next(
        decision
        for decision in clean["decisions"]
        if decision["kind"] == "DEADLOCK_SACRIFICE"
    )
    timeout = next(
        decision
        for decision in clean["decisions"]
        if decision["kind"] == "WAIT_TIMEOUT"
    )
    rollback = next(
        decision
        for decision in clean["decisions"]
        if decision["kind"] == "ACTIVE_ROLLBACK"
    )
    digest_match = (
        clean["run"]["ledger_digest"] == resumed_status["run"]["ledger_digest"]
    )
    wait_match = wait_signature(clean) == wait_signature(resumed_status)
    worker_digest_match = (
        clean["run"]["ledger_digest"] == workers["run"]["ledger_digest"]
    )
    worker_wait_match = wait_signature(clean) == wait_signature(workers)
    report = {
        "ok": (
            tests.returncode == 0
            and digest_match
            and wait_match
            and worker_digest_match
            and worker_wait_match
            and verify_status["ok"]
        ),
        "environment": {
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "network_required": False,
            "container_required": False,
        },
        "tests": {
            "command": "python -m unittest discover -v",
            "returncode": tests.returncode,
            "summary": tests.stderr.strip().splitlines()[-1]
            if tests.stderr.strip()
            else "",
            "output": "artifacts/test-output.txt",
        },
        "clean_run": {
            "status": clean["run"]["status"],
            "ledger_digest": clean["run"]["ledger_digest"],
            "ledger_version": clean["run"]["ledger_version"],
            "logical_clock_ms": clean["run"]["logical_clock_ms"],
            "wait_count": clean["run"]["wait_count"],
            "wait_signature": wait_signature(clean),
            "statuses": {
                tx["tx_key"]: {"status": tx["status"], "reason": tx["reason"]}
                for tx in clean["transactions"]
            },
        },
        "workers": {
            "count": 2,
            "digest_match": worker_digest_match,
            "wait_signature_match": worker_wait_match,
        },
        "crash_recovery": {
            "injected_exit_code": crashed.returncode,
            "watermark_before_resume": before["run"]["watermark_tick"],
            "status_before_resume": before["run"]["status"],
            "digest_match": digest_match,
            "wait_signature_match": wait_match,
            "verify": verify_status,
        },
        "diagnostics": {
            "timeout": {
                "tx_key": timeout["tx_key"],
                "raw_wait_ms": timeout["basis"]["raw_wait_ms"],
                "released": timeout["basis"]["held_locks_released"],
            },
            "deadlock": {
                "cycle": deadlock["cycle"],
                "victim": deadlock["tx_key"],
                "basis": {
                    "transaction_no": deadlock["basis"]["victim_transaction_no"],
                    "wait_raw_ms": deadlock["basis"]["victim_wait_raw_ms"],
                    "deviation_units": deadlock["basis"]["victim_deviation_units"],
                    "error_upper_bound": deadlock["basis"][
                        "victim_error_upper_bound"
                    ],
                },
                "ledger_sensitive": deadlock["ledger_sensitive"],
                "sensitivity": deadlock["sensitivity"],
            },
            "active_rollback": {
                "tx_key": rollback["tx_key"],
                "reason": rollback["reason"],
            },
        },
        "samples": {
            "scenario": str(SCENARIO.relative_to(ROOT)),
            "seed": json.loads((SAMPLES / "manifest.json").read_text(encoding="utf-8"))[
                "seed"
            ],
        },
    }
    report_path = ARTIFACTS / "acceptance-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    for path in (clean_dir, workers_dir, crash_dir):
        if path.exists():
            shutil.rmtree(path)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

