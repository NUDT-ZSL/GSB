"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from .engine import apply_patch, decide_round, edit, load_workspace, resume
from .errors import (
    LOCK_ERROR,
    SCHEMA_ERROR,
    VERIFY_MISMATCH,
    ControlError,
)
from .store import Store
from .util import load_json_file, pretty_json


def _store(args: argparse.Namespace) -> Store:
    return Store(args.db)


def _print(value: Any) -> None:
    sys.stdout.write(pretty_json(value))
    sys.stdout.write("\n")


def _cmd_load(args: argparse.Namespace) -> int:
    result = load_workspace(
        data_dir=args.data_dir,
        out_dir=Path(args.db).parent,
        samples_file=args.samples,
        db_path=args.db,
        reset=args.reset,
    )
    _print(result)
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    payload = load_json_file(args.patch)
    result = apply_patch(
        _store(args),
        payload,
        expected_version=args.expected_version,
        dry_run=args.dry_run,
    )
    _print(result)
    return 0


def _cmd_edit(args: argparse.Namespace) -> int:
    payload = load_json_file(args.patch)
    result = edit(_store(args), payload, commit=not args.dry_run)
    _print(result)
    return 0


def _cmd_decide(args: argparse.Namespace) -> int:
    overrides: dict[str, dict[str, Any]] = {}
    for workload in args.fail_workload or []:
        overrides[workload] = {
            "success": False,
            "reason": "injected_action_failure",
        }
    result = decide_round(
        _store(args),
        round_id=args.round_id,
        at_ms=args.at_ms,
        workloads=args.workload,
        expected_version=args.expected_version,
        outcome_overrides=overrides,
        crash_after_start=args.crash_after_start,
    )
    _print(result)
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    result = resume(_store(args), crash_after_start=args.crash_after_start)
    _print(result)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    _print(_store(args).status())
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    result = _store(args).verify()
    _print(result)
    return 0 if result["ok"] else 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m elastic_control",
        description="Deterministic metric-driven autoscaling control plane",
    )
    parser.add_argument(
        "--db",
        default=str(Path("out") / "control.db"),
        help="SQLite control database path (default: out/control.db)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show Python traceback for unexpected failures",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    load = sub.add_parser("load", help="validate and load policy/sample files")
    load.add_argument("--data-dir", default="samples", help="policy sample directory")
    load.add_argument("--samples", default=None, help="sample stream JSON path")
    load.add_argument("--reset", action="store_true", help="replace an existing database")
    load.set_defaults(func=_cmd_load)

    apply = sub.add_parser("apply", help="apply an incremental policy patch")
    apply.add_argument("--patch", required=True, help="patch JSON path")
    apply.add_argument("--expected-version", type=int, default=None)
    apply.add_argument("--dry-run", action="store_true")
    apply.set_defaults(func=_cmd_apply)

    edit = sub.add_parser("edit", help="edit policy objects through a patch")
    edit.add_argument("--patch", required=True, help="patch JSON path")
    edit.add_argument("--dry-run", action="store_true")
    edit.set_defaults(func=_cmd_edit)

    decide = sub.add_parser("decide", help="commit one logical decision round")
    decide.add_argument("--round-id", required=True)
    decide.add_argument("--at-ms", type=int, required=True)
    decide.add_argument(
        "--workload",
        action="append",
        default=None,
        help="workload to process (repeatable; default: all)",
    )
    decide.add_argument("--expected-version", type=int, default=None)
    decide.add_argument(
        "--fail-workload",
        action="append",
        default=None,
        help="inject a post-start failure for this workload (repeatable)",
    )
    decide.add_argument(
        "--crash-after-start",
        action="store_true",
        help="simulate forced termination after the started phase is committed",
    )
    decide.set_defaults(func=_cmd_decide)

    resume_cmd = sub.add_parser("resume", help="recover pending action phases")
    resume_cmd.add_argument(
        "--crash-after-start",
        action="store_true",
        help="simulate another forced termination during recovery",
    )
    resume_cmd.set_defaults(func=_cmd_resume)

    status = sub.add_parser("status", help="print the current decision snapshot")
    status.set_defaults(func=_cmd_status)

    verify = sub.add_parser("verify", help="verify persisted ledgers and snapshot")
    verify.set_defaults(func=_cmd_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "decide" and args.at_ms < 0:
            raise ControlError(
                SCHEMA_ERROR,
                "--at-ms must be non-negative",
                {"at_ms": args.at_ms},
            )
        return int(args.func(args))
    except ControlError as exc:
        sys.stderr.write(pretty_json(exc.as_dict()))
        sys.stderr.write("\n")
        if args.debug:
            raise
        if exc.code == VERIFY_MISMATCH:
            return 4
        if exc.code == LOCK_ERROR:
            return 5
        return 3 if "VERSION" in exc.code or "CONFLICT" in exc.code else 2
    except (OSError, PermissionError) as exc:
        sys.stderr.write(
            pretty_json(
                {
                    "ok": False,
                    "error_code": "FILESYSTEM_ERROR",
                    "message": str(exc),
                    "details": {},
                }
            )
        )
        sys.stderr.write("\n")
        if args.debug:
            raise
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
