#!/usr/bin/env python3
"""Run the deterministic sample and the full test suite, then emit a report."""

from __future__ import annotations

import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("GW_FAILPOINT", None)
    return subprocess.run(
        [sys.executable, "-m", "gateway", *args],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )


def parse(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    if completed.returncode != 0:
        raise SystemExit(
            f"command failed ({completed.returncode}): {completed.stderr}\n{completed.stdout}"
        )
    return json.loads(completed.stdout)


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        db = Path(temp) / "sample.db"
        loaded = parse(
            run(
                [
                    "load",
                    "--db",
                    str(db),
                    "--services",
                    str(ROOT / "samples" / "services.json"),
                    "--requests",
                    str(ROOT / "samples" / "requests.json"),
                    "--config",
                    str(ROOT / "samples" / "config.json"),
                    "--clock-steps",
                    str(ROOT / "samples" / "clock_steps.json"),
                ]
            )
        )
        dispatched = parse(
            run(
                [
                    "dispatch",
                    "--db",
                    str(db),
                    "--plan",
                    str(ROOT / "samples" / "clock_steps.json"),
                ]
            )
        )
        status = parse(run(["status", "--db", str(db)]))
        verified = parse(run(["verify", "--db", str(db)]))
        version_before_replay = status["ledger_version"]
        repeated = parse(
            run(
                [
                    "dispatch",
                    "--db",
                    str(db),
                    "--plan",
                    str(ROOT / "samples" / "clock_steps.json"),
                ]
            )
        )
        status_after = parse(run(["status", "--db", str(db)]))
        rounds = {item["round_id"]: item for item in dispatched["rounds"]}

        (ARTIFACTS / "sample_status.json").write_text(
            json.dumps(status, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

        checks = {
            "load_version": loaded["version"],
            "dispatch_ledger_version": dispatched["ledger_version"],
            "verify": verified,
            "budget_exhausted_round": rounds["s2-dispatch-alpha-budget"][
                "budget_exhausted"
            ],
            "budget_not_started": any(
                item.get("reason") == "budget_exhausted"
                for item in rounds["s2-dispatch-alpha-budget"]["not_started"]
            ),
            "active_abort_expired_request_4": any(
                item["request_id"] == 4
                for item in rounds["s5-recover-overdue"]["recovery"]["expired"]
            ),
            "generation_request_2": status["requests"]["2"]["generation"],
            "request_4_status": status["requests"]["4"]["status"],
            "clock_watermark_ms": status["logical_clock_watermark_ms"],
            "global_balance_fixed": status["global_token"]["balance_fixed"],
            "queue_version": status["queue_version"],
            "idempotent_replay": all(
                item["idempotent"] for item in repeated["rounds"]
            ),
            "version_unchanged_after_replay": (
                version_before_replay == status_after["ledger_version"]
            ),
        }

    tests = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=str(ROOT),
        env={**os.environ, "GW_FAILPOINT": ""},
        text=True,
        capture_output=True,
        timeout=180,
    )
    tests_ran = 0
    match = re.search(r"Ran (\d+) tests?", tests.stderr + tests.stdout)
    if match:
        tests_ran = int(match.group(1))
    try:
        import numpy

        numpy_version = numpy.__version__
    except ImportError:
        numpy_version = None

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "environment": {
            "python": platform.python_version(),
            "numpy": numpy_version,
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
        },
        "sample": checks,
        "tests": {
            "command": "python -m unittest discover -s tests -v",
            "returncode": tests.returncode,
            "tests_ran": tests_ran,
            "passed": tests.returncode == 0,
            "summary": (tests.stderr.strip().splitlines() or [""])[-1],
        },
    }
    (ARTIFACTS / "validation_report.json").write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if tests.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
