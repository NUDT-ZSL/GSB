"""Command-line interface for the quota ledger."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from decimal import Decimal
from typing import Any, Sequence

from .errors import E_BAD_EVENT, LedgerError
from .ledger import QuotaLedger, load_configuration
from .utils import canonical_json, read_json
from .validation import parse_event_document


def _loads_json(text: str) -> Any:
    return json.loads(text, parse_float=Decimal)


def _emit(value: Any) -> None:
    sys.stdout.write(canonical_json(value) + "\n")


def _add_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, help="SQLite ledger database path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quota-ledger",
        description="Deterministic hierarchical quota and rate-limit ledger",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate and load configuration")
    _add_db(load_parser)
    load_parser.add_argument("--quotas", required=True)
    load_parser.add_argument("--limiter", required=True)

    write_parser = subparsers.add_parser("write", help="adjudicate one event or an event stream")
    _add_db(write_parser)
    write_group = write_parser.add_mutually_exclusive_group(required=True)
    write_group.add_argument("--event-json")
    write_group.add_argument("--events")
    write_parser.add_argument(
        "--fault-stage",
        choices=(
            "after_rate",
            "after_quota",
            "after_token",
            "after_ledger",
            "after_object",
            "before_commit",
        ),
    )
    write_parser.add_argument(
        "--crash-stage",
        choices=("before_commit", "after_intent"),
        help="test-only forced process termination hook",
    )

    delete_parser = subparsers.add_parser("delete", help="adjudicate delete events")
    _add_db(delete_parser)
    delete_group = delete_parser.add_mutually_exclusive_group(required=True)
    delete_group.add_argument("--event-json")
    delete_group.add_argument("--events")
    delete_parser.add_argument(
        "--fault-stage",
        choices=(
            "after_rate",
            "after_quota",
            "after_token",
            "after_ledger",
            "after_object",
            "before_commit",
        ),
    )
    delete_parser.add_argument(
        "--crash-stage",
        choices=("before_commit", "after_intent"),
    )

    resume_parser = subparsers.add_parser("resume", help="recover and verify the ledger")
    _add_db(resume_parser)

    status_parser = subparsers.add_parser("status", help="emit stable ledger status JSON")
    _add_db(status_parser)

    verify_parser = subparsers.add_parser("verify", help="compare stored ledger with replay")
    _add_db(verify_parser)
    verify_parser.add_argument(
        "--source-events",
        help="optional ordered source stream to cross-check against persisted events",
    )

    return parser


def _event_or_stream_arguments(args: argparse.Namespace) -> list[Any]:
    if args.event_json is not None:
        return [json.loads(args.event_json, parse_float=Decimal)]
    document = read_json(args.events)
    if isinstance(document, list):
        return document
    if isinstance(document, dict) and "events" in document:
        return document["events"]
    return [document]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "load":
            quota_document = read_json(args.quotas)
            limiter_document = read_json(args.limiter)
            report = load_configuration(args.db, quota_document, limiter_document)
            _emit(report)
            return 0

        ledger = QuotaLedger.open(args.db)

        if args.command in {"write", "delete"}:
            raw_events = _event_or_stream_arguments(args)
            events = parse_event_document(raw_events, ledger.quota)
            if args.command == "delete" and any(not event.deleted for event in events):
                raise LedgerError(
                    E_BAD_EVENT,
                    "delete accepts only events with deleted=true",
                )
            for event in events:
                if args.command == "delete":
                    result = ledger.delete(
                        event,
                        fault_stage=args.fault_stage,
                        crash_stage=args.crash_stage,
                    )
                else:
                    result = ledger.write(
                        event,
                        fault_stage=args.fault_stage,
                        crash_stage=args.crash_stage,
                    )
                _emit(result)
            return 0

        if args.command == "resume":
            _emit(ledger.resume())
            return 0

        if args.command == "status":
            _emit(ledger.status())
            return 0

        if args.command == "verify":
            source: list[Any] | None = None
            if args.source_events:
                source = parse_event_document(
                    read_json(args.source_events),
                    ledger.quota,
                )
            _emit(ledger.verify(source))
            return 0

        parser.error(f"unknown command: {args.command}")
        return 2
    except LedgerError as exc:
        _emit(exc.to_dict())
        return exc.exit_code
    except (OSError, json.JSONDecodeError) as exc:
        _emit(
            {
                "ok": False,
                "code": "E_BAD_INPUT",
                "error": "E_BAD_INPUT",
                "message": str(exc),
                "details": {},
            }
        )
        return 2
    except sqlite3.Error as exc:
        _emit(
            {
                "ok": False,
                "code": "E_DATABASE",
                "error": "E_DATABASE",
                "message": str(exc),
                "details": {},
            }
        )
        return 2
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "code": "E_INTERNAL",
                "error": "E_INTERNAL",
                "message": str(exc),
                "details": {},
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

