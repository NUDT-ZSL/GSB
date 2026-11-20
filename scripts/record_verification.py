#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from leasecoord.db import answer_json  # noqa: E402
from scripts.demo import run_demo  # noqa: E402


def _environment() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp:
        connection = sqlite3.connect(Path(temp) / "wal.db")
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        connection.close()
    return {
        "python": sys.version.split()[0],
        "python_ok": sys.version_info >= (3, 11),
        "sqlite": sqlite3.sqlite_version,
        "wal_mode": str(mode).lower(),
    }


def _tests() -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-q"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )
    match = re.search(r"Ran (\d+) tests?", completed.stderr)
    return {
        "ok": completed.returncode == 0,
        "tests": int(match.group(1)) if match else None,
        "status": "OK" if "OK" in completed.stderr else "FAILED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/verification.json")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    first = run_demo(ROOT / "workspace", workers=args.workers)
    first_text = answer_json(first)
    second = run_demo(ROOT / "workspace", workers=args.workers)
    second_text = answer_json(second)
    test_result = _tests()
    artifact = {
        "ok": bool(first["ok"] and first_text == second_text and test_result["ok"]),
        "environment": _environment(),
        "unit_tests": test_result,
        "demo_sha256": hashlib.sha256(first_text.encode("utf-8")).hexdigest(),
        "demo_repeat_identical": first_text == second_text,
        "demo": {
            "initial_leader": first["sample"]["initial_election"]["leader"],
            "failover_leader": first["sample"]["failover_election"]["leader"],
            "failover_clock_ms": first["sample"]["failover_election"]["clock_ms"],
            "precision_sensitive": first["sample"]["failover_election"][
                "precision_sensitive"
            ],
            "precision_gap_ms": first["sample"]["failover_election"]["precision_pair"][
                "gap_ms"
            ],
            "first_rejection_code": first["sample"]["failover_election"][
                "first_rejection"
            ]["code"],
            "boundary_candidates": first["sample"]["boundary_view"][
                "boundary_candidates"
            ],
            "status_repeat_identical": first["sample"]["status_repeat_identical"],
            "verification": first["sample"]["verification"]["verified"],
            "database": first["sample"]["database"],
            "concurrency_ok": first["concurrency"]["ok"],
            "concurrency_workers": first["concurrency"]["workers"],
            "concurrency_unique_election_ids": first["concurrency"][
                "concurrent_bootstrap"
            ]["unique_election_ids"],
            "concurrency_committed_renewals": first["concurrency"][
                "concurrent_renew"
            ]["committed_renewal_rows"],
            "crash_recovery_ok": first["crash_recovery"]["ok"],
            "crash_cases": [
                {
                    "crash_at": case["crash_at"],
                    "exit_code": case["process_exit_code"],
                    "ok": case["ok"],
                }
                for case in first["crash_recovery"]["cases"]
            ],
        },
    }
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(artifact, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(artifact, sort_keys=True, indent=2, ensure_ascii=False))
    return 0 if artifact["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
