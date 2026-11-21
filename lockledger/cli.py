"""Command-line interface for run, resume, status, and verify."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .database import build_sample_database
from .engine import CrashPlan, Engine
from .errors import LedgerError, fail
from .fixed import fraction_text
from .reference import serial_replay_check, verify_status
from .samples import write_samples
from .storage import Store
from .util import (
    atomic_write_text,
    canonical_json,
    check_workspace,
    ensure_within,
    sha256_text,
)
from .validation import load_scenario, validate_scenario

SCENARIO_COPY = "scenario.json"
SAMPLE_DB = "sample.sqlite"


def _json_output(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _error_output(error: LedgerError) -> None:
    _json_output({"ok": False, "error": error.as_dict()})


def _attach_config(status: dict[str, Any], scenario: Any) -> dict[str, Any]:
    config = scenario.config
    status["config"] = {
        "clock_step_ms": config.clock_step_ms,
        "scale": fraction_text(config.scale),
        "tolerance": fraction_text(config.tolerance),
        "error_budget": fraction_text(config.error_budget),
        "max_concurrency": config.max_concurrency,
        "deadlock_check_period_ms": config.deadlock_check_period_ms,
        "max_rounds": config.max_rounds,
        "logical_horizon_ms": config.logical_horizon_ms,
    }
    return status


def _load_workdir_scenario(workdir: Path):
    scenario_path = workdir / SCENARIO_COPY
    if not scenario_path.exists():
        fail("E_NO_RUN", f"scenario copy is missing: {scenario_path}")
    return load_scenario(scenario_path)


def _reset_workdir(workdir: Path, workspace: Path) -> None:
    if workdir == workspace:
        fail("E_BAD_WORKDIR", "workdir must be a dedicated subdirectory")
    ensure_within(workspace, workdir)
    if workdir.exists():
        for child in workdir.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        workdir.mkdir(parents=True)


def _prepare_run(
    sample_path: Path,
    workdir: Path,
    workspace: Path,
    *,
    reset: bool,
) -> Any:
    try:
        raw_document = json.loads(sample_path.read_text(encoding="utf-8"))
    except OSError as exc:
        fail("E_SAMPLE_READ", str(exc), str(sample_path))
    except json.JSONDecodeError as exc:
        fail("E_BAD_JSON", str(exc), f"{sample_path}:{exc.lineno}:{exc.colno}")
    scenario = validate_scenario(raw_document, source_path=str(sample_path))
    ensure_within(workspace, workdir)
    check_workspace(workspace)
    if reset:
        _reset_workdir(workdir, workspace)
    else:
        workdir.mkdir(parents=True, exist_ok=True)
        if (workdir / "ledger.sqlite").exists():
            fail("E_RUN_EXISTS", f"ledger already exists in {workdir}")
    scenario_copy = workdir / SCENARIO_COPY
    atomic_write_text(
        scenario_copy,
        json.dumps(raw_document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    copied = load_scenario(scenario_copy)
    if copied.source_sha256 != scenario.source_sha256:
        fail("E_INTERNAL", "scenario copy hash changed", exit_code=4)
    build_sample_database(scenario.database, workdir / SAMPLE_DB)
    store = Store(workdir)
    run_id = f"run-{scenario.source_sha256[:16]}"
    Engine(scenario, store).initialize(run_id)
    return copied


def _run_worker(workdir_text: str) -> dict[str, Any]:
    workdir = Path(workdir_text)
    scenario = _load_workdir_scenario(workdir)
    store = Store(workdir)
    Engine(scenario, store).run()
    return {"pid": os.getpid(), "ok": True}


def _execute_workers(workdir: Path, workers: int) -> list[dict[str, Any]]:
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_run_worker, args=(str(workdir),))
        for _ in range(workers)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join()
    failed = [
        {"pid": process.pid, "exitcode": process.exitcode}
        for process in processes
        if process.exitcode != 0
    ]
    if failed:
        fail(
            "E_WORKER_FAILED",
            "one or more concurrent workers failed",
            exit_code=4,
            details={"workers": failed},
        )
    return [
        {"worker": index + 1, "ok": process.exitcode == 0}
        for index, process in enumerate(processes)
    ]


def _recompute_ledger_digest(
    conn: sqlite3.Connection, version: int, pointer: Any
) -> str:
    transactions = [
        {
            "tx_key": row["tx_key"],
            "tx_no": row["tx_no"],
            "isolation_level": row["isolation_level"],
            "wait_budget_ms": row["wait_budget_ms"],
            "max_retries": row["max_retries"],
            "status": row["status"],
            "reason": row["reason"],
            "next_op": row["next_op"],
        }
        for row in conn.execute(
            "SELECT tx_key, tx_no, isolation_level, wait_budget_ms, max_retries,"
            " status, reason, next_op FROM ledger_transactions"
            " WHERE version=? ORDER BY tx_no",
            (version,),
        )
    ]
    holders = [
        {
            "holder_id": row["holder_id"],
            "tx_key": row["tx_key"],
            "resource_key": row["resource_key"],
            "mode": row["mode"],
            "acquire_seq": row["acquire_seq"],
        }
        for row in conn.execute(
            "SELECT holder_id, tx_key, resource_key, mode, acquire_seq"
            " FROM ledger_lock_holders WHERE version=? ORDER BY holder_id",
            (version,),
        )
    ]
    queue = [
        {
            "queue_id": row["queue_id"],
            "enqueue_seq": row["enqueue_seq"],
            "tx_key": row["tx_key"],
            "resource_key": row["resource_key"],
            "mode": row["mode"],
            "status": row["status"],
            "enqueue_tick": row["enqueue_tick"],
            "deadline_tick": row["deadline_tick"],
        }
        for row in conn.execute(
            "SELECT queue_id, enqueue_seq, tx_key, resource_key, mode, status,"
            " enqueue_tick, deadline_tick FROM ledger_lock_queue"
            " WHERE version=? ORDER BY enqueue_seq",
            (version,),
        )
    ]
    waits = [
        {
            "registration_seq": row["registration_seq"],
            "tx_key": row["tx_key"],
            "op_index": row["op_index"],
            "raw_wait_ms": row["raw_wait_ms"],
            "fixed_units": row["fixed_units"],
            "remainder": row["remainder"],
            "wait_outcome": row["wait_outcome"],
        }
        for row in conn.execute(
            "SELECT registration_seq, tx_key, op_index, raw_wait_ms, fixed_units,"
            " remainder, wait_outcome FROM ledger_wait_records"
            " WHERE version=? ORDER BY registration_seq",
            (version,),
        )
    ]
    decisions = [
        {
            "decision_seq": row["decision_seq"],
            "kind": row["kind"],
            "tx_key": row["tx_key"],
            "reason": row["reason"],
            "ledger_sensitive": row["ledger_sensitive"],
        }
        for row in conn.execute(
            "SELECT decision_seq, kind, tx_key, reason, ledger_sensitive"
            " FROM ledger_decisions WHERE version=? ORDER BY decision_seq",
            (version,),
        )
    ]
    payload = {
        "tick": pointer["tick"],
        "watermark_tick": pointer["watermark_tick"],
        "transactions": transactions,
        "holders": holders,
        "queue": queue,
        "waits": waits,
        "decisions": decisions,
    }
    return sha256_text(canonical_json(payload))


def command_run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace = Path(args.workspace).resolve()
    sample_path = ensure_within(workspace, Path(args.sample))
    workdir = ensure_within(workspace, Path(args.workdir))
    workers = args.workers
    if workers < 1 or workers > 64:
        fail("E_BAD_WORKERS", "workers must be between 1 and 64")
    if args.crash_at:
        if workers != 1:
            fail("E_BAD_CRASH", "crash injection requires --workers 1")
        if args.crash_tick < 0 or args.crash_occurrence < 1:
            fail(
                "E_BAD_CRASH",
                "crash tick must be non-negative and occurrence must be positive",
            )
    scenario = _prepare_run(
        sample_path,
        workdir,
        workspace,
        reset=not args.no_reset,
    )
    crash_plan = None
    if args.crash_at:
        crash_plan = CrashPlan(
            phase=args.crash_at,
            tick=args.crash_tick,
            occurrence=args.crash_occurrence,
        )
    if workers == 1:
        engine = Engine(scenario, Store(workdir), crash_plan=crash_plan)
        status = engine.run()
    else:
        worker_results = _execute_workers(workdir, workers)
        status = Store(workdir).status(scenario.config.error_budget)
        status["workers"] = worker_results
    _attach_config(status, scenario)
    status["command"] = "run"
    return status, 0


def command_resume(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace = Path(args.workspace).resolve()
    workdir = ensure_within(workspace, Path(args.workdir))
    scenario = _load_workdir_scenario(workdir)
    store = Store(workdir)
    if not store.exists():
        fail("E_NO_RUN", f"ledger does not exist: {store.db_path}")
    workers = args.workers
    if workers < 1 or workers > 64:
        fail("E_BAD_WORKERS", "workers must be between 1 and 64")
    check_workspace(workspace)
    if workers == 1:
        status = Engine(scenario, store).run()
    else:
        worker_results = _execute_workers(workdir, workers)
        status = store.status(scenario.config.error_budget)
        status["workers"] = worker_results
    _attach_config(status, scenario)
    status["command"] = "resume"
    return status, 0


def command_status(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace = Path(args.workspace).resolve()
    workdir = ensure_within(workspace, Path(args.workdir))
    scenario = _load_workdir_scenario(workdir)
    status = Store(workdir).status(scenario.config.error_budget)
    _attach_config(status, scenario)
    status["command"] = "status"
    return status, 0


def command_verify(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace = Path(args.workspace).resolve()
    workdir = ensure_within(workspace, Path(args.workdir))
    scenario = _load_workdir_scenario(workdir)
    store = Store(workdir)
    status = store.status(scenario.config.error_budget)
    result = verify_status(status, scenario)
    checks: list[dict[str, Any]] = []
    try:
        checks.append(serial_replay_check(status, scenario, workdir))
    except Exception as exc:
        checks.append(
            {
                "check": "serial_replay",
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    try:
        sample_path = workdir / SAMPLE_DB
        if not sample_path.exists():
            raise sqlite3.OperationalError(f"missing sample database: {sample_path}")
        uri = sample_path.resolve().as_uri() + "?mode=ro"
        sample_conn = sqlite3.connect(uri, uri=True)
        try:
            integrity = sample_conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            sample_conn.close()
        checks.append({"check": "sample_db_integrity", "ok": integrity == "ok"})
    except sqlite3.Error as exc:
        checks.append(
            {"check": "sample_db_integrity", "ok": False, "error": str(exc)}
        )
    conn = store.read_conn()
    try:
        meta = {
            row["key"]: row["value"]
            for row in conn.execute("SELECT key, value FROM meta ORDER BY key")
        }
        checks.append(
            {
                "check": "scenario_hash",
                "ok": meta.get("scenario_sha256") == scenario.source_sha256,
            }
        )
        pointer = store.current_pointer(conn)
        state = conn.execute(
            "SELECT ledger_version, run_status FROM run_state WHERE singleton=1"
        ).fetchone()
        checks.append(
            {
                "check": "ledger_pointer",
                "ok": bool(pointer["state_digest"]),
            }
        )
        recomputed_digest = _recompute_ledger_digest(
            conn, int(pointer["version"]), pointer
        )
        checks.append(
            {
                "check": "ledger_digest",
                "ok": recomputed_digest == pointer["state_digest"],
                "expected": pointer["state_digest"],
                "actual": recomputed_digest,
            }
        )
        checks.append(
            {
                "check": "pointer_version",
                "ok": int(pointer["version"]) == int(state["ledger_version"]),
            }
        )
        orphan_resolved = conn.execute(
            "SELECT COUNT(*) FROM lock_queue q"
            " LEFT JOIN wait_records w"
            " ON w.tx_key=q.tx_key AND w.op_index=q.op_index"
            " AND w.operation_version=q.operation_version"
            " WHERE q.status IN ('GRANTED','TIMEOUT','SACRIFICED')"
            " AND w.registration_seq IS NULL"
        ).fetchone()[0]
        checks.append(
            {
                "check": "queue_wait_linkage",
                "ok": orphan_resolved == 0,
                "orphan_resolved_waits": orphan_resolved,
            }
        )
        orphan_acquisitions = conn.execute(
            "SELECT COUNT(*) FROM op_commits c"
            " LEFT JOIN wait_records w"
            " ON w.registration_seq=c.wait_registration_seq"
            " WHERE c.outcome='WAITED_AND_ACQUIRED'"
            " AND (c.wait_registration_seq IS NULL OR w.registration_seq IS NULL)"
        ).fetchone()[0]
        checks.append(
            {
                "check": "op_commit_wait_linkage",
                "ok": orphan_acquisitions == 0,
                "orphan_acquisitions": orphan_acquisitions,
            }
        )
        wait_count = conn.execute("SELECT COUNT(*) FROM wait_records").fetchone()[0]
        decision_count = conn.execute(
            "SELECT COUNT(*) FROM decisions"
        ).fetchone()[0]
        checks.append(
            {
                "check": "snapshot_counts",
                "ok": (
                    int(pointer["wait_count"]) == int(wait_count)
                    and int(pointer["decision_count"]) == int(decision_count)
                ),
                "pointer_wait_count": int(pointer["wait_count"]),
                "operational_wait_count": int(wait_count),
                "pointer_decision_count": int(pointer["decision_count"]),
                "operational_decision_count": int(decision_count),
            }
        )
    finally:
        conn.rollback()
        conn.close()
    result["checks"] = checks
    result["ok"] = result["ok"] and all(check["ok"] for check in checks)
    result["command"] = "verify"
    return result, 0 if result["ok"] else 3


def command_generate_samples(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace = Path(args.workspace).resolve()
    out_dir = ensure_within(workspace, Path(args.out))
    check_workspace(workspace)
    result = write_samples(out_dir)
    result["command"] = "generate-samples"
    return result, 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lockledger",
        description="Deterministic transaction lock-wait precision ledger diagnostics",
    )
    parser.add_argument(
        "--workspace",
        default=str(Path.cwd()),
        help="workspace boundary (default: current directory)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="start and run a scenario")
    run_parser.add_argument("--sample", required=True)
    run_parser.add_argument("--workdir", default=".lockledger")
    run_parser.add_argument("--workers", type=int, default=1)
    run_parser.add_argument("--no-reset", action="store_true")
    run_parser.add_argument(
        "--crash-at",
        choices=["enqueue", "grant", "abandon", "operation"],
        default=None,
    )
    run_parser.add_argument("--crash-tick", type=int, default=0)
    run_parser.add_argument("--crash-occurrence", type=int, default=1)

    resume_parser = subparsers.add_parser("resume", help="resume a persisted run")
    resume_parser.add_argument("--workdir", default=".lockledger")
    resume_parser.add_argument("--workers", type=int, default=1)

    status_parser = subparsers.add_parser("status", help="print stable status JSON")
    status_parser.add_argument("--workdir", default=".lockledger")

    verify_parser = subparsers.add_parser("verify", help="verify truth and invariants")
    verify_parser.add_argument("--workdir", default=".lockledger")

    generate_parser = subparsers.add_parser(
        "generate-samples", help="regenerate fixed local samples"
    )
    generate_parser.add_argument("--out", default="samples")
    return parser


COMMANDS = {
    "run": command_run,
    "resume": command_resume,
    "status": command_status,
    "verify": command_verify,
    "generate-samples": command_generate_samples,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result, exit_code = COMMANDS[args.command](args)
        _json_output(result)
        return exit_code
    except LedgerError as exc:
        _error_output(exc)
        return exc.exit_code
    except KeyboardInterrupt:
        _json_output(
            {
                "ok": False,
                "error": {"code": "E_INTERRUPTED", "message": "interrupted"},
            }
        )
        return 130
    except Exception as exc:
        _json_output(
            {
                "ok": False,
                "error": {
                    "code": "E_INTERNAL",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            }
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())

