#!/usr/bin/env python3
"""Reproduce the main acceptance checks and write a JSON verification report."""

from __future__ import annotations

import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lockfair.engine import LockFairEngine  # noqa: E402
from lockfair.errors import BusinessError, ErrorCode  # noqa: E402
from lockfair.models import load_bundle, parse_bundle  # noqa: E402


DEMO = ROOT / "samples" / "demo.json"
STALE = ROOT / "samples" / "stale_generation.json"
REPORT = ROOT / "artifacts" / "verification_report.json"


def run_crash(db_path: Path, crash_point: str) -> subprocess.CompletedProcess[str]:
    code = f"""
import os
from lockfair.engine import LockFairEngine
os.environ["LOCKFAIR_CRASH_POINT"] = {crash_point!r}
engine = LockFairEngine({str(db_path)!r})
engine.execute_round(round_no=1, request_id="verification-crash")
"""
    env = os.environ.copy()
    env.pop("LOCKFAIR_CRASH_POINT", None)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def run_idempotency_process(db_path: Path) -> subprocess.CompletedProcess[str]:
    code = f"""
import json
from lockfair.engine import LockFairEngine
result = LockFairEngine({str(db_path)!r}).execute_round(
    round_no=1,
    request_id="verification-parallel",
)
print(json.dumps({{
    "version": result["version"],
    "hash": result["state_hash"],
    "round": result["round_no"],
    "replay": result.get("idempotent_replay", False),
}}))
"""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=45,
    )


def compact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "round_no": summary["round_no"],
        "logical_clock_ms": summary["logical_clock_ms"],
        "budget_ms": summary["budget_ms"],
        "used_budget_ms": summary["used_budget_ms"],
        "grants": [
            {
                "seq": item["seq"],
                "lock_key": item["lock_key"],
                "holder": item["holder"],
                "lease_generation": item["lease_generation"],
                "start_clock_ms": item["start_clock_ms"],
                "expires_at_ms": item["expires_at_ms"],
                "status": item["status"],
                "forced_yield": item["forced_yield"],
            }
            for item in summary["grants"]
        ],
        "expired": [
            {
                "seq": item["seq"],
                "lock_key": item["lock_key"],
                "released_at_ms": item["released_at_ms"],
            }
            for item in summary["expired"]
        ],
        "deferred": [
            {
                "seq": item["seq"],
                "first_trigger": item["first_trigger"],
                "deferred_rounds": item["deferred_rounds"],
            }
            for item in summary["deferred"]
        ],
        "forced_yields": summary["forced_yields"],
        "fairness": summary["fairness"],
        "errors": summary["errors"],
    }


def main() -> int:
    bundle = load_bundle(DEMO)
    with tempfile.TemporaryDirectory(prefix="lockfair-verify-") as directory:
        root = Path(directory)

        # 1. Deterministic sample trajectory.
        db = root / "demo.sqlite"
        engine = LockFairEngine(db)
        engine.initialize(bundle, force=True)
        round_records = []
        for _ in range(6):
            snapshot = engine.execute_round()
            round_records.append(
                {
                    "state_version": snapshot["version"],
                    "state_hash": snapshot["state_hash"],
                    "summary": compact_summary(snapshot["summary"]),
                }
            )
        final = engine.get_state()
        forced_with_comparison = [
            item
            for record in round_records
            for item in record["summary"]["forced_yields"]
            if item.get("compared_application_seq") is not None
        ]
        over_budget = [
            record["summary"]["round_no"]
            for record in round_records
            if record["summary"]["used_budget_ms"] > record["summary"]["budget_ms"]
        ]
        occupancy_violations = [
            identity
            for identity, value in final["state"]["holders"].items()
            if value["occupied_count"] < 0
            or value["occupied_count"] > value["max_concurrent_grants"]
        ]

        # 2. Error-code chain from a stale lease generation.
        stale_db = root / "stale.sqlite"
        stale_engine = LockFairEngine(stale_db)
        stale_engine.initialize(STALE, force=True)
        stale_snapshot = stale_engine.execute_round()

        # 3. Invalid input remains side-effect free at the validator boundary.
        invalid_raw = bundle.to_dict()
        invalid_raw["resources"].append(dict(invalid_raw["resources"][0]))
        try:
            parse_bundle(invalid_raw)
            invalid_code = None
        except BusinessError as exc:
            invalid_code = exc.code.value

        # 4. Crash after durable intent, then replay exactly once.
        clean_db = root / "clean.sqlite"
        clean_engine = LockFairEngine(clean_db)
        clean_engine.initialize(bundle, force=True)
        clean_snapshot = clean_engine.execute_round(round_no=1, request_id="verification-crash")
        crash_db = root / "crash.sqlite"
        LockFairEngine(crash_db).initialize(bundle, force=True)
        crash_process = run_crash(crash_db, "after_prepare")
        crash_engine = LockFairEngine(crash_db)
        recovery = crash_engine.recover()
        recovered_snapshot = crash_engine.get_state()

        # 5. Multiple processes submit the same round; all observe one snapshot.
        parallel_db = root / "parallel.sqlite"
        LockFairEngine(parallel_db).initialize(bundle, force=True)
        parallel_processes = [run_idempotency_process(parallel_db) for _ in range(6)]
        parallel_results = [json.loads(process.stdout) for process in parallel_processes]
        parallel_versions = sorted({item["version"] for item in parallel_results})
        parallel_hashes = sorted({item["hash"] for item in parallel_results})
        conn = sqlite3.connect(str(parallel_db))
        try:
            parallel_rounds = conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
            parallel_grants = conn.execute("SELECT COUNT(*) FROM grants").fetchone()[0]
            duplicate_generations = conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT lock_key, lease_generation FROM grants
                    GROUP BY lock_key, lease_generation HAVING COUNT(*) > 1
                )
                """
            ).fetchone()[0]
        finally:
            conn.close()

        checks = {
            "sample_rounds_committed": len(round_records),
            "budget_never_exceeded": not over_budget,
            "holder_occupancy_within_cap": not occupancy_violations,
            "forced_yield_with_high_priority_comparison": bool(forced_with_comparison),
            "stale_generation_code": [
                item["code"] for item in stale_snapshot["summary"]["errors"]
            ],
            "invalid_input_code": invalid_code,
            "crash_process_exit": crash_process.returncode,
            "recovery_replayed_jobs": recovery["prepared_jobs"],
            "recovery_hash_matches_clean": recovered_snapshot["state_hash"] == clean_snapshot["state_hash"],
            "parallel_unique_versions": parallel_versions,
            "parallel_unique_hashes": len(parallel_hashes),
            "parallel_round_rows": parallel_rounds,
            "parallel_grant_rows": parallel_grants,
            "parallel_duplicate_generations": duplicate_generations,
        }

        report = {
            "ok": (
                not over_budget
                and not occupancy_violations
                and bool(forced_with_comparison)
                and invalid_code == ErrorCode.DUPLICATE_RESOURCE_KEY.value
                and crash_process.returncode == 86
                and recovered_snapshot["state_hash"] == clean_snapshot["state_hash"]
                and parallel_versions == [2]
                and len(parallel_hashes) == 1
                and parallel_rounds == 1
                and duplicate_generations == 0
            ),
            "environment": {
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
                "platform": platform.platform(),
            },
            "sample": {
                "path": str(DEMO.relative_to(ROOT)),
                "bundle_hash": bundle.canonical_hash(),
                "resources": len(bundle.resources),
                "holders": len(bundle.holders),
                "applications": len(bundle.applications),
                "config": bundle.config.to_dict(),
            },
            "checks": checks,
            "rounds": round_records,
            "final_state": {
                "round_no": final["round_no"],
                "logical_clock_ms": final["logical_clock_ms"],
                "state_version": final["version"],
                "state_hash": final["state_hash"],
                "active_leases": {
                    key: value["active"]
                    for key, value in final["state"]["resources"].items()
                    if value.get("active")
                },
                "holders": {
                    key: {
                        "occupied_count": value["occupied_count"],
                        "max_concurrent_grants": value["max_concurrent_grants"],
                    }
                    for key, value in final["state"]["holders"].items()
                },
            },
            "stale_generation_run": {
                "errors": stale_snapshot["summary"]["errors"],
                "granted": [item["seq"] for item in stale_snapshot["summary"]["grants"]],
            },
            "recovery": {
                "crash_point": "after_prepare",
                "process_exit": crash_process.returncode,
                "recovery_result": recovery,
                "clean_state_hash": clean_snapshot["state_hash"],
                "recovered_state_hash": recovered_snapshot["state_hash"],
            },
            "parallel_idempotency": {
                "processes": len(parallel_results),
                "results": parallel_results,
                "round_rows": parallel_rounds,
                "grant_rows": parallel_grants,
                "duplicate_generations": duplicate_generations,
            },
        }
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report["checks"], ensure_ascii=False, indent=2))
    print(f"report: {REPORT}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
