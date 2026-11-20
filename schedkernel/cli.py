from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import KernelError
from .fslock import atomic_write_json
from .service import SchedulerService


DEFAULT_DB = "state/scheduler.db"
DEFAULT_LOCK = "state/scheduler.lock"
DEFAULT_TMP = ".tmp"


def _emit(value: dict[str, Any], out: str | None = None) -> None:
    if out:
        atomic_write_json(out, value)
    text = json.dumps(
        value,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    )
    sys.stdout.write(text + "\n")


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--lock", default=DEFAULT_LOCK)
    parser.add_argument("--tmp", default=DEFAULT_TMP)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schedkernel",
        description="Deterministic local dynamic-batching scheduler",
    )
    parser.add_argument("--version", action="version", version="schedkernel 1.0")
    sub = parser.add_subparsers(dest="command", required=True)

    load_parser = sub.add_parser("load", help="validate and load fixed snapshots")
    _common(load_parser)
    load_parser.add_argument("--model", default="samples/model.json")
    load_parser.add_argument(
        "--replicas", default="samples/replicas.json"
    )
    load_parser.add_argument("--requests", default="samples/requests.json")
    load_parser.add_argument("--precision", default="samples/precision.json")

    for name in ("dispatch", "resume"):
        dispatch_parser = sub.add_parser(
            name, help=f"{name} prepared and pending windows"
        )
        _common(dispatch_parser)
        dispatch_parser.add_argument(
            "--crash-at",
            choices=(
                "inside_window_commit",
                "after_window_commit",
                "after_prepare_windows",
                "after_all_commit",
                "after_commit_before_settle",
                "after_settle",
            ),
            default=None,
        )

    status_parser = sub.add_parser("status", help="emit stable status JSON")
    _common(status_parser)
    status_parser.add_argument("--details", action="store_true")
    status_parser.add_argument("--out", default=None)

    verify_parser = sub.add_parser("verify", help="verify ledger consistency")
    _common(verify_parser)
    verify_parser.add_argument("--out", default=None)

    fail_parser = sub.add_parser(
        "fail", help="record a terminal execution failure for a committed request"
    )
    _common(fail_parser)
    fail_parser.add_argument("--request-id", required=True)
    fail_parser.add_argument("--attempts", type=int, default=1)
    fail_parser.add_argument("--error", default="EXECUTION_ERROR")
    fail_parser.add_argument("--out", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = SchedulerService(args.db, args.lock, args.tmp)
    try:
        if args.command == "load":
            result = service.load_files(
                args.model,
                args.replicas,
                args.requests,
                args.precision,
            )
        elif args.command in ("dispatch", "resume"):
            result = (
                service.dispatch(args.crash_at)
                if args.command == "dispatch"
                else service.resume(args.crash_at)
            )
        elif args.command == "status":
            result = service.status(details=args.details)
        elif args.command == "verify":
            result = service.verify()
        elif args.command == "fail":
            result = service.record_failure(
                args.request_id,
                attempts=args.attempts,
                error=args.error,
            )
        else:
            parser.error("unknown command")
            return 2
    except KernelError as exc:
        error = {"ok": False, "error": exc.to_dict()}
        sys.stderr.write(
            json.dumps(
                error,
                sort_keys=True,
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
        return 2
    except Exception as exc:
        error = {
            "ok": False,
            "error": {
                "code": "E_INTERNAL",
                "message": str(exc),
                "details": {"type": type(exc).__name__},
            },
        }
        sys.stderr.write(
            json.dumps(
                error,
                sort_keys=True,
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
        return 3

    out = getattr(args, "out", None)
    try:
        _emit(result, out=out)
    except BrokenPipeError:
        return 0
    if args.command == "verify" and not result.get("ok", False):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

