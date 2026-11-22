"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .errors import BusinessError, ErrorCode
from .storage import Store
from .validation import document_summary, load_document


def _configure_utf8() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ic-ledger",
        description="Interrupt-coalescing fixed-point accounting ledger",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact JSON instead of indented JSON",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate and load a JSON sample")
    load_parser.add_argument("sample", help="sample JSON path")
    load_parser.add_argument("--db", required=True, help="SQLite database path")
    load_parser.add_argument(
        "--version",
        type=int,
        default=None,
        help="explicit parameter batch version",
    )

    run_parser = subparsers.add_parser("run", help="advance uncommitted records")
    run_parser.add_argument("--db", required=True, help="SQLite database path")
    run_parser.add_argument(
        "--queue",
        action="append",
        default=None,
        metavar="IFACE:QUEUE",
        help="limit execution to a queue (repeatable)",
    )
    run_parser.add_argument(
        "--max-records",
        type=int,
        default=None,
        help="commit at most this many record attempts",
    )

    diagnose_parser = subparsers.add_parser(
        "diagnose", help="attribute budget exceedances from committed ledgers"
    )
    diagnose_parser.add_argument("--db", required=True, help="SQLite database path")
    diagnose_parser.add_argument(
        "--queue",
        action="append",
        default=None,
        metavar="IFACE:QUEUE",
        help="limit diagnosis to a queue (repeatable)",
    )

    resume_parser = subparsers.add_parser(
        "resume", help="finish staged parameter imports and pending records"
    )
    resume_parser.add_argument("--db", required=True, help="SQLite database path")
    resume_parser.add_argument(
        "--max-records", type=int, default=None, help="record attempt limit"
    )

    status_parser = subparsers.add_parser("status", help="emit stable ledger status")
    status_parser.add_argument("--db", required=True, help="SQLite database path")

    verify_parser = subparsers.add_parser(
        "verify", help="replay records and compare all committed ledgers"
    )
    verify_parser.add_argument("--db", required=True, help="SQLite database path")
    verify_parser.add_argument(
        "--queue",
        action="append",
        default=None,
        metavar="IFACE:QUEUE",
        help="limit verification to a queue (repeatable)",
    )
    return parser


def _emit(result: dict[str, Any], compact: bool) -> None:
    if compact:
        text = json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    else:
        text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write(text + "\n")


def _validate_max_records(value: int | None) -> None:
    if value is not None and value < 0:
        raise BusinessError(
            ErrorCode.USAGE,
            "--max-records must be non-negative",
        )


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8()
    parser = _parser()
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    compact_requested = "--compact" in raw_argv
    raw_argv = [item for item in raw_argv if item != "--compact"]
    args = parser.parse_args(raw_argv)
    args.compact = bool(args.compact or compact_requested)
    try:
        if args.command == "load":
            document = load_document(args.sample)
            with Store(args.db) as store:
                imported = store.import_document(
                    document,
                    requested_version=args.version,
                    source="load",
                )
            _emit(
                {
                    "dataset": document_summary(document),
                    "imported": imported.as_dict(),
                    "ok": True,
                },
                args.compact,
            )
            return 0
        if args.command == "run":
            _validate_max_records(args.max_records)
            with Store(args.db) as store:
                result = store.run_queues(
                    args.queue,
                    max_records=args.max_records,
                )
            _emit(result, args.compact)
            return 0
        if args.command == "diagnose":
            with Store(args.db) as store:
                result = store.diagnose(selectors=args.queue)
            _emit(result, args.compact)
            return 0
        if args.command == "resume":
            _validate_max_records(args.max_records)
            with Store(args.db) as store:
                recovered = store.recover_pending()
                run_result = store.run_queues(max_records=args.max_records)
                status = store.status()
            _emit(
                {
                    "ok": run_result["ok"],
                    "recovered": recovered,
                    "run": run_result,
                    "status": status,
                },
                args.compact,
            )
            return 0
        if args.command == "status":
            with Store(args.db) as store:
                result = store.status()
            _emit(result, args.compact)
            return 0
        if args.command == "verify":
            with Store(args.db) as store:
                result = store.verify(selectors=args.queue)
            _emit(result, args.compact)
            return 0 if result["ok"] else 3
        parser.error(f"unknown command {args.command}")
        return 2
    except BusinessError as exc:
        _emit({"error": exc.as_dict(), "ok": False}, args.compact)
        return 2
    except KeyboardInterrupt:
        _emit(
            {
                "error": {
                    "code": "E_INTERRUPTED",
                    "context": {},
                    "message": "interrupted",
                },
                "ok": False,
            },
            args.compact,
        )
        return 130
    except Exception as exc:  # pragma: no cover - final CLI safety net
        _emit(
            {
                "error": {
                    "code": ErrorCode.INTERNAL,
                    "context": {"type": type(exc).__name__},
                    "message": str(exc),
                },
                "ok": False,
            },
            args.compact,
        )
        return 2
