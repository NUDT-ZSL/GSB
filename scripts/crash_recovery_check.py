#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from leasecoord.config import read_json  # noqa: E402
from leasecoord.db import answer_json  # noqa: E402
from leasecoord.engine import CRASH_EXIT_CODE, Engine  # noqa: E402


def _clean_runtime(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for name in ("coord.db", "coord.db-wal", "coord.db-shm", ".coord.lock"):
        (workspace / name).unlink(missing_ok=True)


def _db_snapshot(workspace: Path) -> dict[str, Any]:
    connection = sqlite3.connect(workspace / "coord.db")
    connection.row_factory = sqlite3.Row
    try:
        state = connection.execute(
            "SELECT leader, election_id, clock_text FROM cluster_state WHERE singleton=1"
        ).fetchone()
        prepared = {
            row["status"]: row["n"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS n FROM prepared_ops GROUP BY status ORDER BY status"
            ).fetchall()
        }
        return {
            "leader": state["leader"] if state else None,
            "election_id": state["election_id"] if state else None,
            "watermark_ms": state["clock_text"] if state else None,
            "lease_rows": connection.execute("SELECT COUNT(*) AS n FROM leases").fetchone()["n"],
            "history_rows": connection.execute(
                "SELECT COUNT(*) AS n FROM lease_history"
            ).fetchone()["n"],
            "prepared_statuses": prepared,
        }
    finally:
        connection.close()


def _run_crashing_elect(workspace: Path, crash_at: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "leasecoord",
            "elect",
            "--workspace",
            str(workspace),
            "--clock",
            "0",
            "--crash-at",
            crash_at,
            "--compact",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
    )


def run_case(workspace: Path, crash_at: str) -> dict[str, Any]:
    sample = ROOT / "workspace"
    _clean_runtime(workspace)
    engine = Engine(workspace)
    engine.load(
        read_json(sample / "nodes.json"),
        read_json(sample / "election_config.json"),
        clock="0",
        reset=True,
    )
    crashed = _run_crashing_elect(workspace, crash_at)
    before = _db_snapshot(workspace)
    resumed = engine.resume(clock="0")
    after = _db_snapshot(workspace)
    if crash_at in {"after_prepare", "after_final_insert"}:
        engine.elect(clock="0")
    verification = engine.verify()
    expect_cleanup = crash_at in {"after_prepare", "after_final_insert"}
    ok = (
        crashed.returncode == CRASH_EXIT_CODE
        and verification["verified"]
        and (
            (
                expect_cleanup
                and before["history_rows"] == 0
                and before["prepared_statuses"].get("PREPARED") == 1
                and len(resumed["cleaned_prepared_ops"]) == 1
                and after["prepared_statuses"].get("CLEANED") == 1
            )
            or (
                not expect_cleanup
                and before["history_rows"] == 3
                and before["prepared_statuses"].get("COMMITTED") == 1
                and before["leader"] == "bravo"
                and not resumed["cleaned_prepared_ops"]
                and after["leader"] == "bravo"
            )
        )
    )
    return {
        "crash_at": crash_at,
        "process_exit_code": crashed.returncode,
        "before_resume": before,
        "resume": {
            "cleaned_prepared_ops": len(resumed["cleaned_prepared_ops"]),
            "confirmed_committed_ops": len(resumed["confirmed_committed_ops"]),
            "leader": resumed["leader"],
        },
        "after_resume": after,
        "verify": verification["verified"],
        "ok": ok,
    }


def run_check(workspace_root: Path) -> dict[str, Any]:
    cases = [
        run_case((workspace_root / "after_prepare").resolve(), "after_prepare"),
        run_case((workspace_root / "after_final_insert").resolve(), "after_final_insert"),
        run_case((workspace_root / "after_commit").resolve(), "after_commit"),
    ]
    return {
        "ok": all(case["ok"] for case in cases),
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-root", default="workspace/runtime_crash")
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    result = run_check(Path(args.workspace_root))
    print(answer_json(result, compact=args.compact))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
