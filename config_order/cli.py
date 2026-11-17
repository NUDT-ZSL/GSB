"""Command-line interface for deterministic diagnosis and replay."""

from __future__ import annotations

import argparse
import sys
from typing import Any, Sequence

from .canonical import pretty_json
from .errors import E_INTERNAL, Issue
from .loader import INJECTIONS
from .logging_utils import log_issues
from .service import run_diagnose, run_list_sessions, run_restore
from .verify import verify_session


DEFAULT_DB = "var/diagnosis.sqlite"
DEFAULT_LOG = "var/diagnosis.jsonl"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="config-order",
        description=(
            "Deterministic multi-source configuration order diagnosis console"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    diagnose = subparsers.add_parser("diagnose", help="load and diagnose a scenario")
    diagnose.add_argument("--input", required=True, help="local scenario JSON")
    diagnose.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    diagnose.add_argument("--log", default=DEFAULT_LOG, help="JSONL log path")
    diagnose.add_argument(
        "--inject",
        choices=sorted(INJECTIONS),
        default="none",
        help="inject a fault through input/parameter mutation only",
    )

    restore = subparsers.add_parser(
        "restore", help="replay and restore a persisted session"
    )
    restore.add_argument("--session-id", required=True)
    restore.add_argument("--db", default=DEFAULT_DB)
    restore.add_argument("--log", default=DEFAULT_LOG)

    verify = subparsers.add_parser(
        "verify", help="independently verify a persisted session"
    )
    verify.add_argument("--session-id", default=None)
    verify.add_argument("--db", default=DEFAULT_DB)

    sessions = subparsers.add_parser("sessions", help="list persisted sessions")
    sessions.add_argument("--db", default=DEFAULT_DB)
    return parser


def _print(value: Any) -> None:
    sys.stdout.write(pretty_json(value) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "diagnose":
            report, code = run_diagnose(
                args.input, args.db, args.log, args.inject
            )
            _print(report)
            return code
        if args.command == "restore":
            report, code = run_restore(args.db, args.session_id, args.log)
            _print(report)
            return code
        if args.command == "verify":
            report = verify_session(args.db, args.session_id)
            _print(report)
            return 0 if report["status"] == "PASS" else 5
        if args.command == "sessions":
            _print(run_list_sessions(args.db))
            return 0
    except Exception as exc:
        issue = Issue(
            code=E_INTERNAL,
            field_path="$",
            message=f"unexpected failure: {type(exc).__name__}: {exc}",
        )
        try:
            log_issues(
                getattr(args, "log", DEFAULT_LOG),
                phase=getattr(args, "command", "unknown"),
                business_code=E_INTERNAL,
                issues=[issue],
            )
        except Exception:
            pass
        _print(
            {
                "schema_version": "1.0",
                "status": "FAILED",
                "business_code": E_INTERNAL,
                "errors": [issue.as_dict()],
            }
        )
        return 70
    parser.error("unreachable command")
    return 70


if __name__ == "__main__":
    raise SystemExit(main())
