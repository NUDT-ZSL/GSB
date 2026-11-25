#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gpu_scheduler.engine import SchedulerEngine  # noqa: E402
from gpu_scheduler.models import load_bundle  # noqa: E402
from gpu_scheduler.storage import SchedulerStore  # noqa: E402
from gpu_scheduler.utils import digest_value  # noqa: E402

RESULTS = ROOT / "results"
DB = RESULTS / "scheduler.db"
RECOVERY_EVIDENCE = RESULTS / "recovery-evidence.json"
CONCURRENCY_EVIDENCE = RESULTS / "concurrency-evidence.json"
SUMMARY = RESULTS / "validation-summary.json"


def run_cli(
    db: Path,
    *args: Any,
    crash_at: Optional[str] = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    if crash_at:
        env["GPU_SCHEDULER_CRASH_AT"] = crash_at
    cmd = [sys.executable, "-m", "gpu_scheduler.cli", "--db", str(db), *[str(a) for a in args]]
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc


def parse_json_output(proc: subprocess.CompletedProcess[str]) -> Dict[str, Any]:
    return json.loads(proc.stdout)


def parse_error(proc: subprocess.CompletedProcess[str]) -> Dict[str, Any]:
    return json.loads(proc.stderr)


def reset_main_db() -> None:
    RESULTS.mkdir(exist_ok=True)
    for suffix in ("", "-wal", "-shm", ".lock"):
        Path(str(DB) + suffix).unlink(missing_ok=True)


def run_main_experiment() -> Dict[str, Any]:
    subprocess.run([sys.executable, str(ROOT / "generator.py")], cwd=ROOT, check=True)
    reset_main_db()
    evidence: Dict[str, Any] = {"steps": {}}

    run_cli(DB, "load")
    grants = {}
    for index in range(1, 6):
        job = f"job-frag-{index:02d}"
        result = parse_json_output(
            run_cli(
                DB, "grant", "--round", f"grant-frag-{index:02d}", "--now", "100",
                "--job", job, "--card", "gpu-frag",
            )
        )
        grants[job] = {
            "lease_id": result["lease_id"],
            "generation": result["generation"],
            "compute_interval": result["compute_interval"],
            "memory_interval": result["memory_interval"],
        }
    evidence["steps"]["fragment_grants"] = grants

    repeat = parse_json_output(
        run_cli(
            DB, "grant", "--round", "grant-frag-01", "--now", "100",
            "--job", "job-frag-01", "--card", "gpu-frag",
        )
    )
    evidence["steps"]["idempotent_repeat"] = {
        "idempotent_replay": repeat.get("idempotent_replay"),
        "lease_id": repeat["lease_id"],
        "ledger_version": repeat["ledger_version"],
    }

    run_cli(DB, "grant", "--round", "small-fill", "--now", "100",
            "--job", "job-small-fill", "--card", "gpu-small")
    for suffix in ("a", "b", "c"):
        run_cli(DB, "grant", "--round", f"prey-{suffix}", "--now", "100",
                "--job", f"job-prey-{suffix}", "--card", "gpu-preempt")

    quota_denied = parse_json_output(
        run_cli(DB, "grant", "--round", "quota-denied", "--now", "101",
                "--job", "job-quota-blocked")
    )
    compute_denied = parse_json_output(
        run_cli(DB, "grant", "--round", "compute-denied", "--now", "102",
                "--job", "job-compute-too-big")
    )
    memory_denied = parse_json_output(
        run_cli(DB, "grant", "--round", "memory-denied", "--now", "103",
                "--job", "job-memory-too-big")
    )
    locked = parse_json_output(
        run_cli(DB, "grant", "--round", "locked-denied", "--now", "104",
                "--job", "job-frag-target", "--card", "gpu-locked")
    )
    evidence["steps"]["decisions"] = {
        "quota": quota_denied["decision"]["constraint"],
        "compute": compute_denied["decision"]["constraint"],
        "memory": memory_denied["decision"]["constraint"],
        "unsplittable": locked["decision"]["constraint"],
    }

    bad_generation = run_cli(
        DB, "renew", "--round", "renew-bad", "--now", "140",
        "--job", "job-frag-01", "--generation", "99", check=False,
    )
    renewed = parse_json_output(
        run_cli(DB, "renew", "--round", "renew-ok", "--now", "150",
                "--job", "job-frag-01", "--generation", "1")
    )
    evidence["steps"]["renew"] = {
        "wrong_generation_code": parse_error(bad_generation)["error_code"],
        "old_generation": renewed["old_generation"],
        "new_generation": renewed["generation"],
    }

    recovered = parse_json_output(run_cli(DB, "recover", "--round", "recover-expired", "--now", "300"))
    evidence["steps"]["recovery"] = {
        "recycled_count": recovered["recycled_count"],
        "jobs": [record["job_id"] for record in recovered["records"]],
        "reasons": sorted({record["reason"] for record in recovered["records"]}),
    }

    reorg = parse_json_output(
        run_cli(DB, "reorganize", "--round", "reorg-ok", "--now", "300",
                "--job", "job-frag-target", "--card", "gpu-frag")
    )["report"]
    evidence["steps"]["reorganization"] = {
        "conclusion": reorg["conclusion"],
        "minimum_migrations": reorg["minimum_migrations"],
        "migration_possible": reorg["migration_possible"],
    }

    plan = parse_json_output(run_cli(DB, "preempt", "--job", "job-high-priority", "--now", "300"))["plan"]
    executed = parse_json_output(
        run_cli(
            DB, "preempt", "--job", "job-high-priority", "--now", "300",
            "--execute", "--round", "preempt-exec", "--plan-digest", plan["plan_digest"],
        )
    )
    evidence["steps"]["preemption"] = {
        "plan_digest": plan["plan_digest"],
        "planned_victims": plan["victim_lease_ids"],
        "executed_victims": executed["victim_lease_ids"],
        "digests_match": plan["plan_digest"] == executed["plan_digest"],
        "refund_count": len(executed["refunds"]),
    }

    shrink = parse_json_output(
        run_cli(DB, "recover", "--round", "quota-shrink", "--now", "310",
                "--reason", "quota_shrink", "--quota-group", "qg-frag",
                "--compute-limit", "40")
    )
    evidence["steps"]["quota_shrink"] = {
        "recycled_count": shrink["recycled_count"],
        "reasons": sorted({r["reason"] for r in shrink["records"]}),
    }

    injection_evidence = {}
    injection_commands = [
        ("interval_overlap", 58, [
            "grant", "--round", "inj-grant-overlap", "--now", "320",
            "--job", "job-inj-overlap", "--card", "gpu-frag",
        ]),
        ("generation_rollback", 58, [
            "renew", "--round", "inj-renew-rollback", "--now", "320",
            "--job", "job-frag-01", "--generation", "2",
        ]),
        ("ledger_imbalance", 58, [
            "recover", "--round", "inj-recover-imbalance", "--now", "320",
        ]),
        ("reorg_digest_mismatch", 58, [
            "reorganize", "--round", "inj-reorg-mismatch", "--now", "320",
            "--job", "job-frag-target", "--card", "gpu-frag",
        ]),
    ]
    for name, expected_exit, args in injection_commands:
        proc = run_cli(DB, *args, check=False)
        error = parse_error(proc)
        if proc.returncode != expected_exit:
            raise RuntimeError(f"injection {name} exit {proc.returncode}: {proc.stderr}")
        injection_evidence[name] = {
            "exit_code": proc.returncode,
            "error_code": error["error_code"],
            "injection_id": error["details"]["injection_id"],
        }
    evidence["steps"]["injections"] = injection_evidence

    status = parse_json_output(run_cli(DB, "status", "--output", str(RESULTS / "status.json")))
    evidence["status_digest"] = digest_value(status)
    verify_proc = run_cli(DB, "verify", "--output", str(RESULTS / "verify.json"))
    verify = parse_json_output(verify_proc)
    evidence["verify_ok"] = verify["ok"]
    evidence["ledger_version"] = status["ledger_version"]
    if not verify["ok"]:
        raise RuntimeError(json.dumps(verify, indent=2))
    return evidence


def operation_status(db: Path) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute(
        "SELECT request_key,status,stage,ledger_version FROM operations ORDER BY rowid"
    )]
    conn.close()
    return rows


def run_recovery_evidence() -> Dict[str, Any]:
    cases = []
    crash_cases = [
        ("grant", "after_intent", ["grant", "--round", "crash", "--now", "100",
                                  "--job", "job-frag-01", "--card", "gpu-frag"], False),
        ("grant", "before_commit", ["grant", "--round", "crash", "--now", "100",
                                    "--job", "job-frag-01", "--card", "gpu-frag"], False),
        ("grant", "after_commit", ["grant", "--round", "crash", "--now", "100",
                                   "--job", "job-frag-01", "--card", "gpu-frag"], False),
        ("renew", "before_commit", ["renew", "--round", "crash-renew", "--now", "150",
                                    "--job", "job-frag-02", "--generation", "1"], True),
        ("renew", "after_commit", ["renew", "--round", "crash-renew", "--now", "150",
                                   "--job", "job-frag-02", "--generation", "1"], True),
        ("recover", "before_commit", ["recover", "--round", "crash-recover", "--now", "300"], True),
        ("recover", "after_commit", ["recover", "--round", "crash-recover", "--now", "300"], True),
        ("reorganize", "before_commit", ["reorganize", "--round", "crash-reorg", "--now", "300",
                                         "--job", "job-frag-target", "--card", "gpu-frag"], True),
        ("reorganize", "after_commit", ["reorganize", "--round", "crash-reorg", "--now", "300",
                                        "--job", "job-frag-target", "--card", "gpu-frag"], True),
    ]
    bundle = load_bundle(ROOT / "data" / "samples")
    for stage, point, args, needs_base in crash_cases:
        with tempfile.TemporaryDirectory(prefix="recovery-case-") as tmp:
            db = Path(tmp) / "recovery.db"
            run_cli(db, "load")
            if needs_base:
                run_cli(db, "grant", "--round", "base", "--now", "100",
                        "--job", "job-frag-02", "--card", "gpu-frag")
            crashed = run_cli(db, *args, crash_at=f"{stage}:{point}", check=False)
            if crashed.returncode != -9:
                raise RuntimeError(f"crash hook did not kill {stage}:{point}: {crashed.stderr}")
            before = [row for row in operation_status(db) if row["request_key"] != "base:grant:job-frag-02"]
            resumed = parse_json_output(run_cli(db, "resume"))
            status = parse_json_output(run_cli(db, "status"))
            invariants = SchedulerEngine(SchedulerStore(db), bundle).verify_current_invariants()
            cases.append(
                {
                    "stage": stage,
                    "point": point,
                    "exit_code": crashed.returncode,
                    "operation_before_resume": before[-1] if before else None,
                    "recovered": resumed["recovered"],
                    "pending_operations": status["pending_operations"],
                    "invariants_ok": invariants["ok"],
                }
            )
            if status["pending_operations"] or not invariants["ok"]:
                raise RuntimeError(f"recovery case failed: {stage}:{point} {invariants}")
    report = {"ok": True, "case_count": len(cases), "cases": cases}
    RECOVERY_EVIDENCE.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def run_concurrency_evidence() -> Dict[str, Any]:
    db = RESULTS / "concurrency-demo.db"
    for suffix in ("", "-wal", "-shm", ".lock"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    run_cli(db, "load")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    duplicate_cmd = [
        sys.executable, "-m", "gpu_scheduler.cli", "--db", str(db),
        "grant", "--round", "mp-shared", "--now", "100",
        "--job", "job-mp-01", "--card", "gpu-mp",
    ]
    procs = [subprocess.Popen(duplicate_cmd, cwd=ROOT, env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(6)]
    duplicate_results = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(err)
        duplicate_results.append(json.loads(out))

    distinct_cmds = [
        [sys.executable, "-m", "gpu_scheduler.cli", "--db", str(db),
         "grant", "--round", "mp-distinct", "--now", "100",
         "--job", f"job-mp-{i:02d}", "--card", "gpu-mp"]
        for i in range(2, 7)
    ]
    procs = [subprocess.Popen(cmd, cwd=ROOT, env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for cmd in distinct_cmds]
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(err)

    status = parse_json_output(run_cli(db, "status"))
    active = [
        lease for lease in status["leases"].values()
        if lease["card_id"] == "gpu-mp" and lease["status"] == "active"
    ]
    report = {
        "ok": len({item["lease_id"] for item in duplicate_results}) == 1 and len(active) == 2,
        "duplicate_submissions": len(duplicate_results),
        "duplicate_lease_ids": sorted({item["lease_id"] for item in duplicate_results}),
        "active_mp_lease_count": len(active),
        "used_compute": status["cards"]["gpu-mp"]["used_compute"],
        "used_memory": status["cards"]["gpu-mp"]["used_memory"],
        "active_jobs": sorted(lease["job_id"] for lease in active),
    }
    CONCURRENCY_EVIDENCE.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if not report["ok"]:
        raise RuntimeError(json.dumps(report, indent=2))
    return report


def main() -> int:
    main_evidence = run_main_experiment()
    recovery = run_recovery_evidence()
    concurrency = run_concurrency_evidence()
    summary = {
        "ok": main_evidence["verify_ok"] and recovery["ok"] and concurrency["ok"],
        "main": main_evidence,
        "recovery_case_count": recovery["case_count"],
        "concurrency": concurrency,
        "artifacts": [
            "results/scheduler.db",
            "results/status.json",
            "results/verify.json",
            "results/recovery-evidence.json",
            "results/concurrency-evidence.json",
        ],
    }
    SUMMARY.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
