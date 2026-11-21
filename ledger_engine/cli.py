from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from .errors import LedgerError
from .jsonutil import stable_dumps, write_text_atomic
from .orchestration import (
    diagnose_round,
    load_patches,
    load_sample,
    round_summary,
    run_pending_round,
    status_document,
)
from .persistence import (
    FileLock,
    connect_ro,
    connect_rw,
    current_state,
    ensure_base_loaded,
    init_schema,
    lock_path,
    pending_round,
    round_info,
)
from .verification import verify_database


def _add_common(parser: argparse.ArgumentParser, *, root: bool = False) -> None:
    parser.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        help="SQLite ledger path (default: ./ledger.sqlite3 or LEDGER_DB)",
    )
    parser.add_argument(
        "--sample",
        default=argparse.SUPPRESS,
        help="sample directory (default: ./samples/default or LEDGER_SAMPLE)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledger",
        description="Deterministic fixed-point rounding-ledger engine",
    )
    _add_common(parser, root=True)
    _add_json_options(parser)
    _add_output(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate and load the base sample")
    _add_common(load_parser)
    _add_json_options(load_parser)
    _add_output(load_parser)

    diagnose_parser = subparsers.add_parser(
        "diagnose", help="run or reuse a diagnostic round"
    )
    _add_common(diagnose_parser)
    _add_json_options(diagnose_parser)
    diagnose_parser.add_argument(
        "--patch",
        action="append",
        default=[],
        help="patch JSON to apply (repeatable, applied in order)",
    )
    diagnose_parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="internal parallel workers, capped by max_concurrency",
    )
    _add_output(diagnose_parser)

    resume_parser = subparsers.add_parser(
        "resume", help="resume a pending diagnostic round"
    )
    _add_common(resume_parser)
    _add_json_options(resume_parser)
    resume_parser.add_argument("--workers", type=int, default=1)
    _add_output(resume_parser)

    status_parser = subparsers.add_parser(
        "status", help="print the stable status document"
    )
    _add_common(status_parser)
    _add_json_options(status_parser)
    _add_output(status_parser)

    verify_parser = subparsers.add_parser(
        "verify", help="verify ledger and truth comparisons"
    )
    _add_common(verify_parser)
    _add_json_options(verify_parser)
    _add_output(verify_parser)
    return parser


def _add_json_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--compact",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit compact, sorted JSON",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help="include traceback text for unexpected failures",
    )


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--out",
        default=argparse.SUPPRESS,
        help="write the JSON document atomically to this path",
    )


def _resolve_defaults(args: argparse.Namespace) -> None:
    if not hasattr(args, "db") or args.db is None:
        args.db = os.environ.get("LEDGER_DB", "ledger.sqlite3")
    if not hasattr(args, "sample") or args.sample is None:
        args.sample = os.environ.get("LEDGER_SAMPLE", "samples/default")
    if not hasattr(args, "compact"):
        args.compact = False
    if not hasattr(args, "debug"):
        args.debug = False
    if not hasattr(args, "out"):
        args.out = None


def _emit(args: argparse.Namespace, document: dict[str, Any]) -> None:
    text = stable_dumps(document, pretty=not args.compact)
    if getattr(args, "out", None):
        write_text_atomic(args.out, text)
    sys.stdout.write(text)
    sys.stdout.flush()


def _load_command(args: argparse.Namespace) -> dict[str, Any]:
    sample = load_sample(args.sample)
    with FileLock(lock_path(args.db)):
        connection = connect_rw(args.db)
        try:
            init_schema(connection)
            _, base_version = ensure_base_loaded(connection, sample)
            state = current_state(connection)
        finally:
            connection.close()
    return {
        "base_version": base_version,
        "logical_clock": state["logical_clock"],
        "ok": True,
        "param_scales": {
            name: spec.scale
            for name, spec in sorted(sample.config.parameters.items())
        },
        "schema": "infra-ledger-load/v1",
        "seed": sample.seed,
        "service_count": len(sample.services),
        "services": list(sample.topological_order()),
        "signature": sample.signature,
    }


def _diagnose_command(args: argparse.Namespace) -> dict[str, Any]:
    sample = load_sample(args.sample)
    patches = load_patches(args.patch)
    if args.workers < 1:
        raise LedgerError("E_BAD_ARGUMENT", "--workers must be at least one")
    if not Path(args.db).exists() and patches:
        from .orchestration import _preflight_patches

        _preflight_patches(sample, sample, patches)
    with FileLock(lock_path(args.db)):
        connection = connect_rw(args.db)
        try:
            init_schema(connection)
            summary = diagnose_round(
                connection,
                sample=sample,
                patches=patches,
                workers=args.workers,
            )
        finally:
            connection.close()
    summary = dict(summary)
    summary["schema"] = "infra-ledger-diagnose/v1"
    return summary


def _resume_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.workers < 1:
        raise LedgerError("E_BAD_ARGUMENT", "--workers must be at least one")
    if not Path(args.db).exists():
        raise LedgerError("E_NO_STATE", "ledger database does not exist")
    with FileLock(lock_path(args.db)):
        connection = connect_rw(args.db)
        try:
            init_schema(connection)
            pending = pending_round(connection)
            if pending is not None:
                summary = run_pending_round(
                    connection,
                    workers=args.workers,
                    crash_enabled=True,
                )
                resumed = True
            else:
                state = current_state(connection)
                if state["round_id"] is None:
                    raise LedgerError(
                        "E_NO_PENDING",
                        "there is neither a pending round nor a committed result",
                    )
                info = round_info(connection, state["round_id"])
                if state["version"] != info["version"] or info["status"] != "COMMITTED":
                    raise LedgerError(
                        "E_NO_PENDING",
                        "there is no resumable round for the current result",
                    )
                summary = round_summary(connection, state["round_id"])
                resumed = False
        finally:
            connection.close()
    summary = dict(summary)
    summary["resumed"] = resumed
    summary["schema"] = "infra-ledger-resume/v1"
    return summary


def _status_command(args: argparse.Namespace) -> dict[str, Any]:
    connection = connect_ro(args.db)
    try:
        return status_document(connection)
    finally:
        connection.close()


def _verify_command(args: argparse.Namespace) -> dict[str, Any]:
    connection = connect_ro(args.db)
    try:
        return verify_database(connection)
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _resolve_defaults(args)
    try:
        if args.command == "load":
            document = _load_command(args)
        elif args.command == "diagnose":
            document = _diagnose_command(args)
        elif args.command == "resume":
            document = _resume_command(args)
        elif args.command == "status":
            document = _status_command(args)
        elif args.command == "verify":
            document = _verify_command(args)
        else:
            raise LedgerError("E_BAD_ARGUMENT", "unknown command")
    except LedgerError as exc:
        _emit(args, exc.to_dict())
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - defensive shell boundary
        details: dict[str, Any] = {}
        if args.debug:
            details["traceback"] = traceback.format_exc()
        _emit(
            args,
            {
                "error": {
                    "code": "E_INTERNAL",
                    "details": details,
                    "message": str(exc),
                },
                "ok": False,
            },
        )
        return 3

    _emit(args, document)
    if document.get("schema") == "infra-ledger-verify/v1" and not document.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
