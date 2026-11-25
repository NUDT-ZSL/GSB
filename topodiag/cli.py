"""Command line interface for the local orchestration diagnostics component."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any

from .diagnostics import status_workspace, verify_workspace
from .engine import build_workspace
from .errors import BusinessError
from .sample import DEFAULT_SEED, default_round_budget_ms, generate_samples
from .scheduler import recover_workspace, schedule_workspace
from .util import append_json_log


def _print(value: Any) -> None:
    sys.stdout.write(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="topodiag",
        description=(
            "Deterministic cross-version policy invalidation and topology "
            "recovery diagnostics"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="generate deterministic local samples")
    load.add_argument("--workdir", default="topodiag-data")
    load.add_argument("--seed", type=int, default=DEFAULT_SEED)
    load.add_argument("--force", action="store_true")

    build = subparsers.add_parser(
        "build",
        help="validate inputs, merge policies, and atomically activate a version",
    )
    build.add_argument("--workdir", default="topodiag-data")
    build.add_argument("--clock", type=int, default=None)
    build.add_argument("--policy-package", default=None)
    build.add_argument(
        "--crash-at",
        choices=["policy-stage", "policy-pointer"],
        default=None,
        help="test-only fault injection; exits the process before commit",
    )

    schedule = subparsers.add_parser(
        "schedule",
        help="adjudicate and atomically instantiate requests",
    )
    schedule.add_argument("--workdir", default="topodiag-data")
    schedule.add_argument(
        "--round-budget-ms",
        type=int,
        default=default_round_budget_ms(),
    )
    schedule.add_argument("--workers", type=int, default=4)
    schedule.add_argument(
        "--crash-at",
        choices=["dependency", "resource"],
        default=None,
        help="test-only fault injection; exits the process mid-flight",
    )

    recover = subparsers.add_parser(
        "recover",
        help="repair interrupted state and replay only missing work",
    )
    recover.add_argument("--workdir", default="topodiag-data")
    recover.add_argument(
        "--round-budget-ms",
        type=int,
        default=default_round_budget_ms(),
    )
    recover.add_argument("--workers", type=int, default=4)

    status = subparsers.add_parser("status", help="emit stable JSON status")
    status.add_argument("--workdir", default="topodiag-data")

    verify = subparsers.add_parser("verify", help="verify all persisted invariants")
    verify.add_argument("--workdir", default="topodiag-data")
    return parser


def _run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workdir = _path(args.workdir)
    if args.command == "load":
        if args.seed < 0:
            raise BusinessError(
                "INVALID_SEED",
                "seed must be a non-negative integer",
                seed=args.seed,
            )
        result = generate_samples(workdir, seed=args.seed, force=args.force)
        return result, 0
    if args.command == "build":
        if args.clock is not None and args.clock < 0:
            raise BusinessError(
                "INVALID_LOGICAL_CLOCK",
                "clock must be a non-negative integer",
                logical_clock=args.clock,
            )
        result = build_workspace(
            workdir,
            clock_ms=args.clock,
            policy_package=(
                _path(args.policy_package) if args.policy_package else None
            ),
            crash_at=args.crash_at,
        )
        return result, 0
    if args.command == "schedule":
        if args.round_budget_ms < 0:
            raise BusinessError(
                "INVALID_ROUND_BUDGET",
                "round budget must be non-negative",
                round_budget_ms=args.round_budget_ms,
            )
        if args.workers < 1:
            raise BusinessError(
                "INVALID_WORKER_COUNT",
                "workers must be at least one",
                workers=args.workers,
            )
        result = schedule_workspace(
            workdir,
            round_budget_ms=args.round_budget_ms,
            workers=args.workers,
            crash_at=args.crash_at,
        )
        return result, 0
    if args.command == "recover":
        result = recover_workspace(
            workdir,
            round_budget_ms=args.round_budget_ms,
            workers=args.workers,
        )
        return result, 0
    if args.command == "status":
        return status_workspace(workdir), 0
    if args.command == "verify":
        result = verify_workspace(workdir)
        return result, 0 if result["ok"] else 5
    raise AssertionError(f"unhandled command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    workdir = _path(args.workdir)
    try:
        result, exit_code = _run(args)
        append_json_log(
            workdir,
            args.command,
            "completed",
            ok=result.get("ok", True),
            exit_code=exit_code,
            semantic_digest=result.get("semantic_digest"),
            mode=result.get("mode"),
        )
        _print(result)
        return exit_code
    except BusinessError as exc:
        append_json_log(
            workdir,
            args.command,
            "business_error",
            code=exc.code,
            diagnostics=exc.diagnostics,
        )
        _print(exc.to_dict())
        return exc.exit_code
    except sqlite3.Error as exc:
        error = BusinessError(
            "SQLITE_ERROR",
            "SQLite operation failed",
            sqlite_error=str(exc),
        )
        append_json_log(
            workdir,
            args.command,
            "sqlite_error",
            error=str(exc),
        )
        _print(error.to_dict())
        return error.exit_code
    except Exception as exc:  # pragma: no cover - last-resort CLI boundary
        error = BusinessError(
            "INTERNAL_ERROR",
            "Unexpected internal error",
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=traceback.format_exc().splitlines()[-8:],
        )
        append_json_log(
            workdir,
            args.command,
            "internal_error",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        _print(error.to_dict())
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
