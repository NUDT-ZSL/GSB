#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fieldengine import engine  # noqa: E402


def clean(directory: Path, pattern: str = "*") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.glob(pattern):
        if path.is_file():
            path.unlink()


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "fieldengine", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def main() -> int:
    import numpy

    state = ROOT / "state" / "acceptance"
    clean(state)
    subprocess.run([sys.executable, str(ROOT / "tools/generate_samples.py")], cwd=ROOT, check=True)
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    pass_match = re.search(r"(\d+) passed", tests.stdout)
    report: dict = {
        "environment": {
            "python": sys.version.split()[0],
            "numpy": numpy.__version__,
            "sqlite": sqlite3.sqlite_version,
        },
        "tests": {
            "returncode": tests.returncode,
            "passed": int(pass_match.group(1)) if pass_match else None,
            "summary": tests.stdout.strip().splitlines()[-1] if tests.stdout.strip() else "",
        },
    }

    basic_db = state / "basic.sqlite3"
    basic_run = engine.run(ROOT / "samples/basic/experiment.json", basic_db)
    basic_verify = engine.verify(ROOT / "samples/basic/experiment.json", basic_db)
    basic_status = engine.status(basic_db)
    report["basic"] = {
        "run_ok": basic_run["ok"],
        "shards": basic_run["shard_counts"],
        "results": basic_run["results"],
        "verify": basic_verify,
        "status_counts": basic_status["shard_watermark"]["counts"],
        "budget_peak_reserved_bytes": basic_status["budget"]["peak_reserved_bytes"],
        "window_buffer_entries": basic_status["window_buffer"]["entries"],
        "mode": basic_status["run"]["mode"],
    }

    budget_db = state / "budget.sqlite3"
    budget_run = engine.run(ROOT / "samples/budget/experiment.json", budget_db)
    budget_status = engine.status(budget_db)
    report["budget"] = {
        "run_ok": budget_run["ok"],
        "status": budget_run["run"]["status"],
        "error_code": budget_run["run"]["error_code"],
        "shards": budget_run["shard_counts"],
        "results": budget_status["results"],
        "reserved_bytes": budget_status["budget"]["reserved_bytes"],
        "abandon_reasons": sorted(
            {item["reason"] for item in budget_status["budget"]["events"] if item["action"] == "ABANDON"}
        ),
    }

    anomaly_db = state / "anomalies.sqlite3"
    anomaly_run = engine.run(ROOT / "samples/anomalies/experiment.json", anomaly_db)
    anomaly_verify = engine.verify(ROOT / "samples/anomalies/experiment.json", anomaly_db)
    report["anomalies"] = {
        "run_ok": anomaly_run["ok"],
        "verify_ok": anomaly_verify["ok"],
        "fixed_overflow_observations": anomaly_verify["fixed_overflow_observations"],
    }

    precision_db = state / "precision.sqlite3"
    precision_run = engine.run(ROOT / "samples/precision/experiment.json", precision_db)
    precision_verify = engine.verify(ROOT / "samples/precision/experiment.json", precision_db)
    report["precision"] = {
        "run_ok": precision_run["ok"],
        "verify_ok": precision_verify["ok"],
        "precision_sensitive_groups": precision_verify["precision_sensitive_groups"],
    }

    crash_db = state / "crash.sqlite3"
    crash_experiment = ROOT / "samples/crash_running/experiment.json"
    crashed = cli(
        "run",
        "--experiment",
        str(crash_experiment),
        "--db",
        str(crash_db),
    )
    before = engine.status(crash_db)
    resumed = engine.resume(crash_experiment, crash_db)
    resumed_verify = engine.verify(crash_experiment, crash_db)
    after = engine.status(crash_db)
    report["crash_recovery"] = {
        "first_exit": crashed.returncode,
        "before_counts": before["shard_watermark"]["counts"],
        "interrupted_shards": [
            item["shard_id"]
            for item in before["shard_watermark"]["shards"]
            if item["status"] == "RUNNING"
        ],
        "resume_ok": resumed["ok"],
        "after_counts": after["shard_watermark"]["counts"],
        "interrupted_attempts": sum(
            item["interrupted_attempts"] for item in after["shard_watermark"]["shards"]
        ),
        "verify": resumed_verify,
    }
    report["ok"] = bool(
        tests.returncode == 0
        and basic_run["ok"]
        and basic_verify["ok"]
        and not budget_run["ok"]
        and budget_run["run"]["error_code"] == "E_RUN_ABANDONED"
        and anomaly_verify["ok"]
        and precision_verify["ok"]
        and resumed["ok"]
        and resumed_verify["ok"]
    )
    report_path = ROOT / "artifacts" / "acceptance-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
