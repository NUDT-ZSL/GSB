"""Run the full local acceptance suite and write a compact evidence summary."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parents[1]


def environment() -> dict[str, str]:
    value = os.environ.copy()
    value["PYTHONPATH"] = (
        str(ROOT / "src")
        + os.pathsep
        + value.get("PYTHONPATH", "")
    )
    return value


def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        env=environment(),
        text=True,
        capture_output=True,
        check=False,
    )


def parse_success(arguments: list[str]) -> dict[str, Any]:
    completed = run(arguments)
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {completed.stdout} {completed.stderr}"
        )
    return json.loads(completed.stdout)


def main() -> int:
    tests = run(
        ["-m", "unittest", "discover", "-s", "tests", "-t", "."]
    )
    compare = parse_success(
        ["scripts/compare_runs.py", "--manifest", "samples/manifest.json"]
    )
    recovery = parse_success(
        ["scripts/recovery_check.py", "--manifest", "samples/manifest.json"]
    )
    with tempfile.TemporaryDirectory() as temporary:
        database = Path(temporary) / "acceptance.db"
        load = parse_success(
            [
                "-m",
                "qos_pipeline",
                "load",
                "--manifest",
                "samples/manifest.json",
                "--db",
                str(database),
            ]
        )
        shape = parse_success(
            [
                "-m",
                "qos_pipeline",
                "shape",
                "--db",
                str(database),
                "--no-steps",
            ]
        )
        verify = parse_success(
            [
                "-m",
                "qos_pipeline",
                "verify",
                "--db",
                str(database),
                "--no-steps",
            ]
        )
    with sqlite3.connect(":memory:") as connection:
        sqlite_version = connection.execute("SELECT sqlite_version()").fetchone()[0]
    test_lines = [line for line in tests.stderr.strip().splitlines() if line.strip()]
    test_summary = " | ".join(test_lines[-2:]) if test_lines else ""
    output = {
        "ok": (
            tests.returncode == 0
            and compare["ok"]
            and recovery["ok"]
            and verify["ok"]
        ),
        "environment": {
            "python": sys.version.split()[0],
            "sqlite": sqlite_version,
            "numpy": np.__version__,
            "target_runtime": "Windows, Python 3.11+",
        },
        "sample": {
            "sample_id": load["sample_id"],
            "sample_digest": load["sample_digest"],
            "injection_id": load["injection_id"],
            "config_sha256": load["config_digest"],
            "arrivals_sha256": load["arrivals_digest"],
            "events": load["events"],
            "steps": load["total_steps"],
        },
        "unit_tests": {
            "ok": tests.returncode == 0,
            "summary": test_summary,
        },
        "repeat_comparison": compare,
        "crash_recovery": recovery,
        "acceptance_run": {
            "state_digest": shape["status"]["state_digest"],
            "last_step": shape["status"]["last_step"],
            "step_digest": shape["status"]["step_digest"],
            "verify_ok": verify["ok"],
            "verify_complete": verify["complete"],
            "chain_valid": verify["chain_valid"],
            "estimator_comparison": verify["estimator_comparison"],
            "precision_marker_count": len(
                verify["derived"]["precision_sensitive"]
            ),
            "classes": verify["derived"]["classes"],
            "precision_sensitive": verify["derived"]["precision_sensitive"],
        },
    }
    output["final_verification_ok"] = output["ok"]
    artifact = ROOT / "artifacts" / "validation_summary.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": output["ok"],
                "artifact": str(artifact),
                "tests": output["unit_tests"],
                "state_digest": output["acceptance_run"]["state_digest"],
                "estimator_consistent": output["acceptance_run"][
                    "estimator_comparison"
                ]["consistent"],
                "precision_marker_count": output["acceptance_run"][
                    "precision_marker_count"
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

