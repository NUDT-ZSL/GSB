#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from seed_samples import generate  # noqa: E402


def reset_db(path: Path) -> None:
    for candidate in [path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + ".lock")]:
        if candidate.exists():
            candidate.unlink()


def run_cli(db: Path, args: List[str], *, crash: bool = False, check: bool = True) -> Dict[str, Any]:
    command = [sys.executable, str(ROOT / "workbench"), "--sample-dir", str(ROOT / "samples"), "--db", str(db), *args]
    env = dict(os.environ)
    if crash:
        env["WB_CRASH_POINT"] = "checkpoint_after_prepare"
    else:
        env.pop("WB_CRASH_POINT", None)
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, env=env)
    payload: Optional[Dict[str, Any]] = None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {"raw_stdout": completed.stdout, "stderr": completed.stderr}
    if check and completed.returncode != 0:
        raise AssertionError({"args": args, "returncode": completed.returncode, "payload": payload, "stderr": completed.stderr})
    return {"returncode": completed.returncode, "payload": payload}


def summary_status(db: Path) -> Dict[str, Any]:
    result = run_cli(db, ["status"])
    status = result["payload"]["status"]
    return {
        "logical_clock_watermark": status["logical_clock_watermark"],
        "binding_id": (status.get("semantic_version_binding") or {}).get("binding_id"),
        "partition_watermarks": [
            {"partition_key": item["partition_key"], "watermark_offset": item["watermark_offset"], "status": item["status"]}
            for item in status["partition_watermarks"]
        ],
        "cursors": status["subscription_cursors"],
        "closure_count": len(status["invalidation_event_chains"]),
        "patch_count": len(status["patches"]),
        "diagnostic_run_count": len(status["diagnostic_runs"]),
        "recovery": status["recovery"],
    }


def main() -> int:
    samples = generate(ROOT / "samples")
    db = ROOT / "state" / "acceptance.sqlite3"
    crash_db = ROOT / "state" / "acceptance-crash.sqlite3"
    reset_db(db)
    reset_db(crash_db)
    report: Dict[str, Any] = {"ok": True, "steps": {}}

    report["steps"]["load"] = run_cli(db, ["load"])["payload"]["result"]
    report["steps"]["merge"] = run_cli(db, ["merge"])["payload"]["result"]["binding"]["binding_id"]
    diagnose = run_cli(db, ["diagnose"])["payload"]["result"]
    report["steps"]["diagnose_default_counts"] = diagnose["diagnostic_run"]["counts"]
    patch = run_cli(db, ["patch"])["payload"]["result"]
    report["steps"]["patch_replayed"] = {"replayed": patch["replayed"], "patch_count": len(patch["patches"])}

    budget = run_cli(db, ["diagnose", "--budget-ms", "250", "--concurrency", "8"])["payload"]["result"]["diagnostic_run"]
    cap = run_cli(db, ["diagnose", "--budget-ms", "100000", "--concurrency", "1"])["payload"]["result"]["diagnostic_run"]
    report["steps"]["budget_scheduling"] = budget["counts"]
    report["steps"]["concurrency_scheduling"] = cap["counts"]

    package_merge = run_cli(db, ["merge", "--package", str(ROOT / "samples/packages/pkg-002-invalidation")])
    report["steps"]["incremental_package"] = package_merge["payload"]["result"]["binding"]["decision"]
    package_diagnose = run_cli(db, ["diagnose", "--clock-ms", "9000"])["payload"]["result"]
    report["steps"]["incremental_recompute"] = package_diagnose["watermark_recompute"]

    rollback = run_cli(
        db,
        ["merge", "--package", str(ROOT / "samples/packages/pkg-009-rollback")],
        check=False,
    )
    report["steps"]["rollback"] = {"returncode": rollback["returncode"], "code": rollback["payload"]["error"]["code"]}
    if rollback["returncode"] != 3 or rollback["payload"]["error"]["code"] != "WB_SNAPSHOT_VERSION_ROLLBACK":
        report["ok"] = False

    full = run_cli(db, ["merge", "--package", str(ROOT / "samples/packages/pkg-003-full-replacement")])
    report["steps"]["full_package"] = full["payload"]["result"]["binding"]["decision"]
    run_cli(db, ["diagnose", "--clock-ms", "9000"])

    verify = run_cli(db, ["verify"])["payload"]
    report["steps"]["verify"] = {
        "ok": verify["ok"],
        "field_diff_count": verify["result"]["field_diff_count"],
        "current_database": verify["result"]["current_database"],
    }
    if not verify["ok"]:
        report["ok"] = False

    run_cli(crash_db, ["load"])
    crashed = run_cli(crash_db, ["diagnose"], crash=True, check=False)
    report["steps"]["sigkill_returncode"] = crashed["returncode"]
    resume = run_cli(crash_db, ["resume"])["payload"]["result"]
    crash_verify = run_cli(crash_db, ["verify"])["payload"]
    report["steps"]["recovery"] = {
        "resume": resume,
        "verify_ok": crash_verify["ok"],
        "field_diff_count": crash_verify["result"]["field_diff_count"],
    }
    if crashed["returncode"] == 0 or not crash_verify["ok"]:
        report["ok"] = False

    report["status_summary"] = summary_status(db)
    report["crash_status_summary"] = summary_status(crash_db)
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    output = artifacts / "acceptance-report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "report": str(output)}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
