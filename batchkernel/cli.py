from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any

from .config import load_config_bundle
from .engine import load_configuration, resume, schedule, status, verify
from .errors import BusinessError


def _emit(payload: dict[str, Any], pretty: bool = False) -> None:
    if pretty:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    else:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    print(text)


def _add_pretty(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pretty", action="store_true", help="pretty-print sorted JSON")


def _add_sub_state_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir",
        dest="sub_state_dir",
        help="local state directory (may also be supplied before the subcommand)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m batchkernel.cli",
        description="Deterministic local inference dynamic-batching scheduler",
    )
    parser.add_argument("--state-dir", default="state", help="local state directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate and load local JSON inputs")
    _add_sub_state_dir(load_parser)
    load_parser.add_argument("--model")
    load_parser.add_argument("--replicas")
    load_parser.add_argument("--requests")
    load_parser.add_argument("--precision")
    load_parser.add_argument("--reset", action="store_true")
    load_parser.add_argument("--clock", type=int)
    _add_pretty(load_parser)

    schedule_parser = subparsers.add_parser(
        "schedule", help="advance the injected logical clock and run scheduling"
    )
    _add_sub_state_dir(schedule_parser)
    schedule_parser.add_argument("--clock", type=int, required=True)
    _add_pretty(schedule_parser)

    resume_parser = subparsers.add_parser(
        "resume", help="recover planned/applying windows and finish an in-flight target"
    )
    _add_sub_state_dir(resume_parser)
    _add_pretty(resume_parser)

    status_parser = subparsers.add_parser(
        "status", help="emit stable sorted scheduler state"
    )
    _add_sub_state_dir(status_parser)
    _add_pretty(status_parser)

    verify_parser = subparsers.add_parser(
        "verify", help="check persisted scheduler invariants"
    )
    _add_sub_state_dir(verify_parser)
    _add_pretty(verify_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    state_dir = args.sub_state_dir or args.state_dir
    try:
        if args.command == "load":
            bundle = load_config_bundle(
                args.model,
                args.replicas,
                args.requests,
                args.precision,
            )
            result = load_configuration(
                state_dir,
                bundle,
                args.clock,
                args.reset,
            )
            _emit({"ok": True, "command": "load", "result": result}, args.pretty)
            return 0
        if args.command == "schedule":
            result = schedule(state_dir, args.clock)
            _emit({"ok": True, "command": "schedule", "result": result}, args.pretty)
            return 0
        if args.command == "resume":
            result = resume(state_dir)
            _emit({"ok": True, "command": "resume", "result": result}, args.pretty)
            return 0
        if args.command == "status":
            state = status(state_dir)
            _emit({"ok": True, "command": "status", "status": state}, args.pretty)
            return 0
        if args.command == "verify":
            result = verify(state_dir)
            _emit(
                {
                    "ok": result["ok"],
                    "command": "verify",
                    "verification": result,
                },
                args.pretty,
            )
            return 0 if result["ok"] else 1
        parser.error("unknown command")
    except BusinessError as error:
        _emit(
            {
                "ok": False,
                "command": args.command,
                "error": error.as_dict(),
            }
        )
        return error.exit_code
    except sqlite3.DatabaseError as error:
        _emit(
            {
                "ok": False,
                "command": args.command,
                "error": {
                    "code": "E_STATE_CORRUPT",
                    "message": "SQLite state operation failed",
                    "details": {"reason": str(error)},
                },
            }
        )
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

