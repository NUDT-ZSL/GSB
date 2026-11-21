#!/usr/bin/env python3
"""Run deterministic local acceptance checks without network access."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "sample" / "package.json"
PYTHON = sys.executable


def run(db: Path, *args: str, env: dict[str, str] | None = None) -> tuple[int, dict]:
    command = [
        PYTHON,
        "-m",
        "config_engine",
        "--db",
        str(db),
        "--package",
        str(PACKAGE),
        *args,
    ]
    process_env = os.environ.copy()
    process_env.pop("CONFIG_ENGINE_CRASH_AFTER_STEP", None)
    if env:
        process_env.update(env)
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=process_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode == 97 and not completed.stdout:
        return 97, {}
    return completed.returncode, json.loads(completed.stdout)


def states(status: dict) -> dict[str, str]:
    return {
        f"{item['service']}/{item['environment']}": item["state"]
        for item in status["view"]["services"]
    }


def main() -> int:
    checks: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="config-engine-acceptance-") as temporary:
        root = Path(temporary)
        db1 = root / "one.sqlite"
        db2 = root / "two.sqlite"
        for db in (db1, db2):
            run(db, "merge", "--clock-step", "t0")
            run(db, "reconcile", "--clock-step", "t3")
        _, status1 = run(db1, "status")
        _, status2 = run(db2, "status")
        _, verify1 = run(db1, "verify")
        checks.append(
            {
                "name": "two_fresh_runs_have_identical_view",
                "passed": status1["view"] == status2["view"],
            }
        )
        checks.append(
            {
                "name": "two_fresh_runs_have_identical_watermarks",
                "passed": status1["event_streams"] == status2["event_streams"],
            }
        )
        checks.append({"name": "verify_passes", "passed": verify1["ok"]})

        expected = {
            "auth/prod": "active",
            "billing/prod": "tombstoned",
            "cache/dev": "active",
            "legacy/prod": "tombstoned",
            "orders/dev": "active",
            "orders/prod": "active",
            "reports/staging": "expired",
            "search/staging": "tombstoned",
        }
        checks.append(
            {
                "name": "expected_t3_states",
                "passed": states(status1) == expected,
            }
        )

        crashed = root / "crashed.sqlite"
        code, _ = run(
            crashed,
            "merge",
            "--clock-step",
            "t0",
            env={"CONFIG_ENGINE_CRASH_AFTER_STEP": "merge:record_migrations"},
        )
        checks.append({"name": "merge_sigkill_exit_97", "passed": code == 97})
        run(crashed, "resume")
        code, _ = run(
            crashed,
            "reconcile",
            "--clock-step",
            "t3",
            env={"CONFIG_ENGINE_CRASH_AFTER_STEP": "reconcile:event:auto:2"},
        )
        checks.append({"name": "event_sigkill_exit_97", "passed": code == 97})
        run(crashed, "resume")
        _, crashed_status = run(crashed, "status")
        checks.append(
            {
                "name": "recovered_view_matches_uninterrupted",
                "passed": crashed_status["view"] == status1["view"],
            }
        )
        checks.append(
            {
                "name": "recovered_watermarks_match_uninterrupted",
                "passed": crashed_status["event_streams"] == status1["event_streams"],
            }
        )
        _, recovered_verify = run(crashed, "verify")
        checks.append(
            {"name": "recovered_verify_passes", "passed": recovered_verify["ok"]}
        )

    report = {
        "checks": checks,
        "ok": all(item["passed"] for item in checks),
        "python": sys.version.split()[0],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
