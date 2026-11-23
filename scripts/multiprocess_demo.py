#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def command(db_path: Path, *args: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "quota_ledger",
        "--db",
        str(db_path),
        *args,
    ]


def spawn(db_path: Path, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        command(db_path, *args),
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def run(db_path: Path, *args: str) -> dict[str, Any]:
    process = subprocess.run(
        command(db_path, *args),
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if process.returncode not in (0, 1):
        raise RuntimeError(process.stderr + process.stdout)
    return json.loads(process.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description="多进程幂等与守恒演示")
    parser.add_argument(
        "--data-dir",
        default=str(ROOT / "data" / "multiprocess"),
    )
    parser.add_argument(
        "--artifacts-dir",
        default=str(ROOT / "artifacts"),
    )
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    artifacts = Path(args.artifacts_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "ledger.db"
    samples = ROOT / "samples"

    load = run(
        db_path,
        "load",
        "--tenants",
        str(samples / "tenants.json"),
        "--services",
        str(samples / "services.json"),
        "--applications",
        str(samples / "applications.json"),
        "--reset",
    )

    submit_args = [
        "--now-ms",
        "0",
        "--round-id",
        "mp-round",
        "--round-start-ms",
        "0",
        "--round-budget-ms",
        "100",
    ]
    processes = [
        spawn(db_path, "submit", "--application", app_id, *submit_args)
        for app_id in ("app-0001", "app-0002", "app-0003")
    ]
    processes.extend(
        spawn(db_path, "submit", "--application", "app-0001", *submit_args)
        for _ in range(6)
    )
    process_results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=120)
        process_results.append(
            {
                "returncode": process.returncode,
                "stderr": stderr,
                "output": json.loads(stdout) if stdout.strip() else None,
            }
        )
    if any(item["returncode"] != 0 for item in process_results):
        raise RuntimeError(json.dumps(process_results, ensure_ascii=False, indent=2))

    release_processes = [
        spawn(
            db_path,
            "release",
            "--application",
            "app-0002",
            "--now-ms",
            "50",
            "--trigger",
            "MANUAL",
        )
        for _ in range(5)
    ]
    release_results = []
    for process in release_processes:
        stdout, stderr = process.communicate(timeout=120)
        release_results.append(
            {
                "returncode": process.returncode,
                "stderr": stderr,
                "output": json.loads(stdout) if stdout.strip() else None,
            }
        )
    if any(item["returncode"] != 0 for item in release_results):
        raise RuntimeError(json.dumps(release_results, ensure_ascii=False, indent=2))

    status = run(db_path, "status")
    verify = run(db_path, "verify")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        idempotency_counts = {
            "app_0001_quota_admissions": conn.execute(
                """
                SELECT COUNT(*) AS n FROM ledger_entries
                WHERE application_id = 'app-0001' AND entry_type = 'QUOTA_ADMIT'
                """
            ).fetchone()["n"],
            "app_0002_quota_releases": conn.execute(
                """
                SELECT COUNT(*) AS n FROM ledger_entries
                WHERE application_id = 'app-0002' AND entry_type = 'QUOTA_RELEASE'
                """
            ).fetchone()["n"],
            "app_0002_release_events": conn.execute(
                """
                SELECT COUNT(*) AS n FROM release_events
                WHERE application_id = 'app-0002'
                """
            ).fetchone()["n"],
        }
    finally:
        conn.close()
    admitted_counts = [
        application["application_id"]
        for application in status["applications"]
        if application["status"] == "ADMITTED"
    ]
    report = {
        "ok": bool(verify["ok"])
        and idempotency_counts == {
            "app_0001_quota_admissions": 1,
            "app_0002_quota_releases": 1,
            "app_0002_release_events": 1,
        },
        "db_path": str(db_path.resolve()),
        "load_operation": load["operation"],
        "submit_processes": len(processes),
        "duplicate_submit_processes": 6,
        "release_processes": len(release_processes),
        "admitted": admitted_counts,
        "idempotency_counts": idempotency_counts,
        "tenants": status["tenants"],
        "verify": verify,
        "process_errors": [
            result for result in process_results + release_results
            if result["returncode"] != 0
        ],
    }
    target = artifacts / "multiprocess_report.json"
    target.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "report": str(target.resolve()),
                "admitted": admitted_counts,
                "idempotency_counts": idempotency_counts,
                "verify": verify,
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
