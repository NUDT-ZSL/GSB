#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from walrec.engine import Engine  # noqa: E402
from walrec.errors import WalRecError  # noqa: E402
from walrec.sample import parse_sample  # noqa: E402

SAMPLES = ROOT / "samples"
NORMAL = SAMPLES / "normal"
BUDGET = SAMPLES / "budget"
ABNORMAL = SAMPLES / "abnormal"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "walrec", *args],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> int:
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    test_text = tests.stdout + tests.stderr
    passed_match = re.search(r"(\d+) passed", test_text)
    failed_match = re.search(r"(\d+) failed", test_text)

    with tempfile.TemporaryDirectory(prefix="walrec-validation-") as temp:
        work = Path(temp)
        full = Engine(work / "full.db")
        full.load(NORMAL, checkpoint_gen=1)
        full_result = full.apply(NORMAL, no_auto_checkpoint=True)
        incremental = Engine(work / "incremental.db")
        incremental.load(NORMAL, checkpoint_gen=2)
        incremental_result = incremental.apply(NORMAL, no_auto_checkpoint=True)
        comparison = incremental.compare(work / "full.db")

        before = incremental.snapshot()
        repeated = incremental.apply(NORMAL, no_auto_checkpoint=True)
        after = incremental.snapshot()

        budget = Engine(work / "budget.db")
        budget.load(BUDGET, checkpoint_gen=1)
        budget_result = budget.apply(BUDGET)
        budget_statuses = Counter(
            outcome["status"] for outcome in budget_result["outcomes"]
        )

        error_codes: dict[str, str] = {}
        for variant in (
            "bad_wal",
            "chain_break",
            "summary_mismatch",
            "checkpoint_missing",
            "replay_generation",
        ):
            try:
                parse_sample(ABNORMAL / variant)
                error_codes[variant] = "NO_ERROR"
            except WalRecError as exc:
                error_codes[variant] = exc.code

        run_outputs = []
        for index in range(2):
            db_path = work / f"stable-{index}" / "state.db"
            db_path.parent.mkdir()
            load_output = run_cli(
                "load",
                "--sample",
                str(NORMAL),
                "--db",
                str(db_path),
                "--reset",
            )
            apply_output = run_cli(
                "apply",
                "--sample",
                str(NORMAL),
                "--db",
                str(db_path),
            )
            status_output = run_cli(
                "status",
                "--sample",
                str(NORMAL),
                "--db",
                str(db_path),
            )
            run_outputs.append(
                {
                    "apply": apply_output.stdout,
                    "load": load_output.stdout,
                    "status": status_output.stdout,
                }
            )

    try:
        import numpy

        numpy_version: str | None = numpy.__version__
    except ImportError:
        numpy_version = None
    try:
        import pytest

        pytest_version: str | None = pytest.__version__
    except ImportError:
        pytest_version = None

    report = {
        "environment": {
            "numpy": numpy_version,
            "platform": platform.platform(),
            "pytest": pytest_version,
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
        },
        "ok": tests.returncode == 0,
        "scenarios": {
            "budget": {
                "applied_txids": budget_result["applied"],
                "ok": budget_result["ok"],
                "statuses": dict(sorted(budget_statuses.items())),
                "watermark": budget_result["status"]["applied_watermark"],
            },
            "cross_version": {
                "different_rows": comparison["different_row_count"],
                "full_applied": full_result["applied"],
                "incremental_applied": incremental_result["applied"],
                "rows_equal": comparison["ok"],
            },
            "error_codes": error_codes,
            "idempotent_repeat": {
                "applied": repeated["applied"],
                "snapshot_unchanged": before == after,
            },
            "stable_cli_outputs": {
                "apply_equal": run_outputs[0]["apply"] == run_outputs[1]["apply"],
                "load_equal": run_outputs[0]["load"] == run_outputs[1]["load"],
                "status_equal": run_outputs[0]["status"] == run_outputs[1]["status"],
            },
        },
        "tests": {
            "failed": int(failed_match.group(1)) if failed_match else 0,
            "passed": int(passed_match.group(1)) if passed_match else 0,
            "returncode": tests.returncode,
        },
    }
    report_path = ROOT / "artifacts" / "validation-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
