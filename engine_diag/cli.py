"""Argparse command line interface."""

from __future__ import annotations

import argparse
import os
import sqlite3
from typing import Any

from . import __version__
from .errors import DB_ERROR, BusinessError
from .service import (
    DEFAULT_DB,
    build_workspace,
    inject_workspace,
    load_workspace,
    resume_workspace,
    schedule_workspace,
    status_workspace,
    verify_workspace,
)
from .util import stable_json_dumps


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="engine-diag",
        description=(
            "Deterministic execution-order, deadline and cost-model "
            "diagnostics workbench."
        ),
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("ENGINE_DIAG_DB", DEFAULT_DB),
        help=f"SQLite database path (default: {DEFAULT_DB})",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    load = sub.add_parser("load", help="validate and load tasks/stats/plan")
    load.add_argument("--tasks", help="operator task JSONL")
    load.add_argument("--stats", help="platform stats JSON/JSONL")
    load.add_argument("--plan", help="candidate plan JSON")
    load.add_argument(
        "--max-threads",
        type=int,
        default=16,
        help="declared max thread range when the stats file has no envelope",
    )

    build = sub.add_parser("build", help="generate subtasks, costs and ledger")
    build.add_argument("--plan-version")
    build.add_argument(
        "--crash-after",
        type=int,
        default=None,
        help="simulate a hard process death after starting the Nth subtask",
    )

    schedule = sub.add_parser("schedule", help="run budgeted deadline arbitration")
    schedule.add_argument("--plan-version")
    schedule.add_argument("--candidate-id")
    schedule.add_argument("--budget", type=float, required=True)
    schedule.add_argument("--max-concurrency", type=int, default=2)
    schedule.add_argument("--rounds", type=int, default=1)
    schedule.add_argument("--start-clock", type=int, default=0)
    schedule.add_argument("--round-span-ms", type=int, default=100)

    inject = sub.add_parser("inject", help="inject deterministic drift/anomalies")
    inject.add_argument("--plan-version")
    inject.add_argument("--seed", type=int)
    inject.add_argument("--drift-count", type=int, default=4)
    inject.add_argument("--anomaly-count", type=int, default=1)
    inject.add_argument("--duration-steps", type=int, default=3)
    inject.add_argument("--clear", action="store_true")
    inject.add_argument("--late-subtask-id")
    inject.add_argument("--late-digest")

    resume = sub.add_parser("resume", help="recover pending attempts and rebuild")
    resume.add_argument("--plan-version")

    status = sub.add_parser("status", help="emit stable diagnostic JSON")
    status.add_argument("--plan-version")

    verify = sub.add_parser("verify", help="verify deterministic and transactional invariants")
    verify.add_argument("--plan-version")
    return parser


def _dispatch(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.command == "load":
        result = load_workspace(
            db=args.db,
            tasks_path=args.tasks,
            stats_path=args.stats,
            plan_path=args.plan,
            max_threads=args.max_threads,
        )
        return result, 0
    if args.command == "build":
        return (
            build_workspace(
                db=args.db,
                plan_version=args.plan_version,
                crash_after=args.crash_after,
            ),
            0,
        )
    if args.command == "schedule":
        return (
            schedule_workspace(
                db=args.db,
                plan_version=args.plan_version,
                budget=args.budget,
                max_concurrency=args.max_concurrency,
                rounds=args.rounds,
                start_clock_ms=args.start_clock,
                round_span_ms=args.round_span_ms,
                candidate_id=args.candidate_id,
            ),
            0,
        )
    if args.command == "inject":
        return (
            inject_workspace(
                db=args.db,
                plan_version=args.plan_version,
                seed=args.seed,
                drift_count=args.drift_count,
                anomaly_count=args.anomaly_count,
                duration_steps=args.duration_steps,
                clear=args.clear,
                late_subtask_id=args.late_subtask_id,
                late_digest=args.late_digest,
            ),
            0,
        )
    if args.command == "resume":
        return resume_workspace(db=args.db, plan_version=args.plan_version), 0
    if args.command == "status":
        return status_workspace(db=args.db, plan_version=args.plan_version), 0
    if args.command == "verify":
        result = verify_workspace(db=args.db, plan_version=args.plan_version)
        return result, 0 if result["ok"] else 1
    raise BusinessError("INVALID_ARGUMENT", f"unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result, exit_code = _dispatch(args)
    except BusinessError as exc:
        print(stable_json_dumps(exc.as_dict()))
        return exc.exit_code
    except sqlite3.Error as exc:
        error = BusinessError(
            DB_ERROR,
            "SQLite operation failed",
            details={"reason": str(exc)},
        )
        print(stable_json_dumps(error.as_dict()))
        return error.exit_code
    print(stable_json_dumps(result))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

