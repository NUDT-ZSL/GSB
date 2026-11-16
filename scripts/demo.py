from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx

from pipeline.alignment import alignment_report
from pipeline.config import TaskConfig
from pipeline.db import Database
from pipeline.scheduler import run_task

SITE_HOST = "127.0.0.1"
SITE_PORT = 8766
SITE_BASE = f"http://{SITE_HOST}:{SITE_PORT}"
FORCED_EXIT_CODE = 73

MAIN_FAULTS = {
    "always_503": ["beta/BIZ-004"],
    "flaky_503": {"alpha/BIZ-002": 2},
    "always_slow": ["beta/BIZ-005"],
    "flaky_slow": {"alpha/BIZ-003": 1},
    "session_trap": ["alpha/BIZ-006"],
    "flaky_session": {"alpha/BIZ-005": 1},
}
NO_FAULTS = {k: ([] if isinstance(v, list) else {}) for k, v in MAIN_FAULTS.items()}

MAIN_TASK = TaskConfig(task_id="main", sources=["alpha", "beta"],
                       concurrency=4, retry_wait_budget=8)
REPLAY_TASK = replace(MAIN_TASK, task_id="replay")
PROBE_TASK = TaskConfig(task_id="align_probe", sources=["alpha", "beta"],
                        concurrency=2, retry_wait_budget=4, biz_filter=["BIZ-001"])
RECOVERY_TASK = TaskConfig(task_id="recovery", sources=["alpha"],
                           concurrency=1, retry_wait_budget=2)


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((SITE_HOST, port)) != 0


def start_site() -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "site_server", "--host", SITE_HOST, "--port", str(SITE_PORT)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            httpx.get(f"{SITE_BASE}/healthz", timeout=0.5)
            return proc
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("site server did not become ready")


def post_site_config(payload: dict) -> None:
    resp = httpx.post(f"{SITE_BASE}/__config", json=payload, timeout=5)
    resp.raise_for_status()


async def run_in_process(cfg: TaskConfig, db: Database) -> dict:
    async with httpx.AsyncClient(base_url=SITE_BASE) as client:
        return await run_task(cfg, db, client)


def run_worker_subprocess(cfg: TaskConfig, db_path: Path, crash_after: int | None) -> int:
    cfg_path = ROOT / "artifacts" / f"worker_{cfg.task_id}.json"
    cfg_path.write_text(json.dumps({
        "site_base_url": SITE_BASE,
        "task": cfg.to_dict(),
    }))
    env = dict(os.environ)
    if crash_after:
        env["CRASH_AFTER_CONFIRMED"] = str(crash_after)
    proc = subprocess.run(
        [sys.executable, "-m", "pipeline.worker", "--config", str(cfg_path), "--db", str(db_path)],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    return proc.returncode


def run_demo(db_path: Path) -> dict:
    if db_path.exists():
        db_path.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            side.unlink()
    recovery_db_path = Path(str(db_path).replace(".db", "") + "_recovery.db")
    if recovery_db_path.exists():
        recovery_db_path.unlink()
    for suffix in ("-wal", "-shm"):
        side = Path(str(recovery_db_path) + suffix)
        if side.exists():
            side.unlink()
    db = Database(str(db_path))
    summary: dict = {"schema": "catalog-demo-v1"}

    post_site_config({"faults": MAIN_FAULTS, "version_overrides": {}, "slow_seconds": 1.0})
    summary["tasks"] = {}
    summary["tasks"]["main"] = asyncio.run(run_in_process(MAIN_TASK, db))
    summary["tasks"]["replay"] = asyncio.run(run_in_process(REPLAY_TASK, db))

    # Version-switch injection: BIZ-001 is re-served as schema v2 by both
    # sources, then probed so alignment has four variants to compare.
    post_site_config({"faults": NO_FAULTS,
                      "version_overrides": {"alpha/BIZ-001": 2, "beta/BIZ-001": 2}})
    summary["tasks"]["align_probe"] = asyncio.run(run_in_process(PROBE_TASK, db))
    post_site_config({"faults": NO_FAULTS, "version_overrides": {}})

    # Recovery: forced kill after 4 confirmations, resume from checkpoint.
    # The recovery scenario uses its own database so its confirmed-key
    # accounting is not entangled with the main task's dedupe demonstration.
    exit_forced = run_worker_subprocess(RECOVERY_TASK, recovery_db_path, crash_after=4)
    recovery_db = Database(str(recovery_db_path))
    pre_cursor, pre_keys = recovery_db.load_checkpoint("recovery")
    exit_resume = run_worker_subprocess(RECOVERY_TASK, recovery_db_path, crash_after=None)
    recovery_task = recovery_db.get_task("recovery")
    post_keys = [i["item_key"] for i in recovery_db.task_items("recovery") if i["status"] == "confirmed"]
    retained = sorted(set(pre_keys) & set(post_keys))
    summary["worker_exit_codes"] = {
        "main": 0,
        "replay": 0,
        "recovery_forced": exit_forced,
        "recovery_resumed": exit_resume,
    }
    summary["recovery"] = {
        "pre_resume_confirmed_count": len(pre_keys),
        "pre_resume_checkpoint_keys": sorted(pre_keys),
        "pre_resume_cursor": pre_cursor,
        "post_resume_confirmed_count": len(post_keys),
        "post_resume_status": recovery_task["status"],
        "retained_confirmed_count": len(retained),
        "retained_keys_match_pre_resume": sorted(pre_keys) == retained,
    }
    summary["tasks"]["recovery"] = recovery_task["summary"]
    recovery_db.close()

    summary["alignment"] = alignment_report(db, "BIZ-001")

    stats = db.normalized_stats()
    summary["database"] = {
        "normalized_rows": stats["rows"],
        "unique_keys": stats["unique_keys"],
        "duplicate_rows_absent": stats["duplicate_key_groups"] == 0,
        "unique_key_consistent_with_evidence": db.evidence_consistent(),
    }
    main = summary["tasks"]["main"]
    replay = summary["tasks"]["replay"]
    summary["checks"] = {
        "in_flight_budget_observed": main["budgets"]["in_flight_within_budget"]
            and main["budgets"]["in_flight_peak"] >= 2,
        "retry_budget_observed": main["budgets"]["retry_wait_within_budget"],
        "main_failure_classes": main["failure_classes"],
        "main_status": main["status"],
        "deduplicated": replay["duplicate_count"] == main["confirmed_count"]
            and replay["normalized_records_for_task"] == 0,
        "recovery_retained": summary["recovery"]["retained_keys_match_pre_resume"]
            and summary["recovery"]["pre_resume_confirmed_count"] == 4,
        "recovery_status": summary["recovery"]["post_resume_status"],
        "recovery_forced_exit_code": exit_forced,
        "all_required_fields_complete": summary["alignment"]["summary"]["all_required_complete"],
        "replay_status": replay["status"],
        "replay_duplicate_count": replay["duplicate_count"],
    }
    db.close()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic pipeline demo")
    parser.add_argument("--db", default=str(ROOT / "artifacts" / "demo.db"))
    parser.add_argument("--out", default=str(ROOT / "artifacts" / "demo_summary.json"))
    parser.add_argument("--repeat", type=int, default=1,
                        help="run the whole demo N times and verify identical summaries")
    args = parser.parse_args()

    if not port_available(SITE_PORT):
        print(f"port {SITE_PORT} already in use", file=sys.stderr)
        return 2
    site = start_site()
    summaries = []
    try:
        for run_index in range(args.repeat):
            db_path = Path(args.db if args.repeat == 1 else f"{args.db}.{run_index}")
            summaries.append(run_demo(db_path))
        deterministic = all(s == summaries[0] for s in summaries)
    finally:
        site.terminate()
        site.wait(timeout=10)

    out = Path(args.out)
    out.write_text(json.dumps(summaries[0], indent=2, sort_keys=True) + "\n")
    print(json.dumps(summaries[0]["checks"], indent=2, sort_keys=True))
    print(f"summary written to {out}")
    if args.repeat > 1:
        print(f"determinism over {args.repeat} runs: {'OK' if deterministic else 'MISMATCH'}")
        return 0 if deterministic else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
