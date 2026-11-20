#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "schedkernel", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


def parsed(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-dir", default="artifacts/demo/live"
    )
    args = parser.parse_args()
    run_dir = (ROOT / args.run_dir).resolve()
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    db = run_dir / "scheduler.db"
    lock = run_dir / "scheduler.lock"
    tmp = run_dir / "tmp"
    common = ("--db", str(db), "--lock", str(lock), "--tmp", str(tmp))

    load_result = run("load", *common)
    if load_result.returncode != 0:
        sys.stderr.write(load_result.stderr)
        return load_result.returncode
    dispatch_result = run("dispatch", *common)
    if dispatch_result.returncode != 0:
        sys.stderr.write(dispatch_result.stderr)
        return dispatch_result.returncode
    status_result = run(
        "status",
        *common,
        "--details",
        "--out",
        str(run_dir / "status.json"),
    )
    verify_result = run(
        "verify", *common, "--out", str(run_dir / "verify.json")
    )
    report = {
        "load": parsed(load_result),
        "dispatch": parsed(dispatch_result),
        "status_summary": {
            "active_replica_version": parsed(status_result)[
                "active_replica_version"
            ],
            "logical_clock_watermark_ms": parsed(status_result)[
                "logical_clock_watermark_ms"
            ],
            "request_counts": parsed(status_result)["request_counts"],
            "outcome_counts": parsed(status_result)["outcome_counts"],
            "precision_sensitive_total": parsed(status_result)[
                "precision_sensitive_total"
            ],
            "committed_batches_total": parsed(status_result)[
                "committed_batches_total"
            ],
        },
        "verify": parsed(verify_result),
        "artifacts": {
            "database": str(db),
            "status_json": str(run_dir / "status.json"),
            "verify_json": str(run_dir / "verify.json"),
        },
    }
    (run_dir / "run-report.json").write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if verify_result.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

