from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from .errors import BusinessError, ErrorCode
from .models import FailureSpec, json_default
from .parsing import parse_config, parse_manifest, resolve_clock
from .service import Workbench


def emit(payload: Any) -> None:
    json.dump(
        payload,
        sys.stdout,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=json_default,
    )
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tierlab",
        description=(
            "Deterministic local hot/cold tier migration and diagnostics "
            "workbench. Every command requires an injected logical clock."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="validate and load a bucket")
    load.add_argument("--db", required=True, help="SQLite workspace path")
    load.add_argument("--manifest", required=True, help="object manifest JSON")
    load.add_argument("--config", required=True, help="tier config JSON")
    _add_clock_options(load)

    migrate = subparsers.add_parser("migrate", help="plan and execute one round")
    migrate.add_argument("--db", required=True)
    migrate.add_argument("--bucket", required=True)
    migrate.add_argument("--round", type=int, default=None)
    migrate.add_argument(
        "--mode",
        choices=("auto", "exact", "sampled"),
        default=None,
        help="override config.mode for this request",
    )
    migrate.add_argument(
        "--fail-key",
        action="append",
        default=[],
        help="inject a deterministic failure for this object key (repeatable)",
    )
    migrate.add_argument(
        "--fail-count",
        type=int,
        default=0,
        help="inject failure for the first N started migrations",
    )
    migrate.add_argument(
        "--fail-every",
        type=int,
        default=0,
        help="inject failure for every Nth started migration",
    )
    _add_clock_options(migrate)

    resume = subparsers.add_parser(
        "resume",
        help="finalize persisted half-committed batches",
    )
    resume.add_argument("--db", required=True)
    resume.add_argument("--bucket", default=None)

    status = subparsers.add_parser("status", help="emit stable diagnostic JSON")
    status.add_argument("--db", required=True)
    status.add_argument("--bucket", default=None)

    verify = subparsers.add_parser(
        "verify",
        help="verify ledgers, capacities, clock and batch consistency",
    )
    verify.add_argument("--db", required=True)
    verify.add_argument("--bucket", default=None)
    return parser


def _add_clock_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--clock", type=int, default=None)
    group.add_argument(
        "--clock-table",
        "--clock-file",
        dest="clock_table",
        default=None,
    )
    parser.add_argument(
        "--clock-step",
        type=int,
        default=1,
        help="step number to select from --clock-table",
    )


def _failure_spec(args: argparse.Namespace) -> FailureSpec:
    if args.fail_count < 0 or args.fail_every < 0:
        raise BusinessError(
            ErrorCode.INVALID_FIELD,
            "--fail-count and --fail-every must be >= 0",
        )
    return FailureSpec(
        fail_keys=tuple(sorted(set(args.fail_key))),
        fail_count=args.fail_count,
        fail_every=args.fail_every,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "load":
            manifest = parse_manifest(args.manifest)
            config = parse_config(args.config)
            clock = resolve_clock(
                args.clock,
                args.clock_table,
                args.clock_step,
            )
            result = Workbench(args.db).load(manifest, config, clock)
        elif args.command == "migrate":
            clock = resolve_clock(
                args.clock,
                args.clock_table,
                args.clock_step,
            )
            result = Workbench(args.db).migrate(
                bucket=args.bucket,
                clock=clock,
                round_number=args.round,
                requested_mode=args.mode,
                failure_spec=_failure_spec(args),
            )
        elif args.command == "resume":
            result = Workbench(args.db).resume(args.bucket)
        elif args.command == "status":
            result = Workbench(args.db).status(args.bucket)
        elif args.command == "verify":
            result = Workbench(args.db).verify(args.bucket)
        else:
            raise BusinessError(
                ErrorCode.INVALID_FIELD,
                "unknown command",
                {"command": args.command},
            )
    except BusinessError as exc:
        emit({"ok": False, "error": exc.to_dict()})
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - CLI must emit stable JSON.
        emit(
            {
                "ok": False,
                "error": {
                    "code": ErrorCode.INTERNAL,
                    "message": "unexpected internal error",
                    "details": {"error": str(exc)},
                },
            }
        )
        return 1
    emit(result)
    if args.command == "verify" and not result.get("ok", False):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
