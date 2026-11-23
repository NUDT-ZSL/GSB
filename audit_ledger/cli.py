from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

from .engine import AuditEngine, FaultPlan
from .errors import AuditError
from .storage import AuditStore
from .validation import load_bundle


def _print(payload: dict[str, Any]) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _non_negative(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m audit_ledger",
        description="Local audited event persistence with logical-time quotas.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate and load sample inputs")
    load_parser.add_argument("--db", required=True)
    load_parser.add_argument("--events", required=True)
    load_parser.add_argument("--quotas", required=True)
    load_parser.add_argument("--policy", required=True)

    write_parser = subparsers.add_parser("write", help="write queued events")
    write_parser.add_argument("--db", required=True)
    write_parser.add_argument("--rounds", type=_positive, default=None)
    write_parser.add_argument("--tenant", default=None)
    write_parser.add_argument(
        "--advance-clock-ms", type=_non_negative, default=0
    )
    write_parser.add_argument(
        "--abandon-seq", action="append", type=int, default=[]
    )
    write_parser.add_argument(
        "--fail-key", action="append", default=[]
    )
    write_parser.add_argument("--fail-count", type=_positive, default=1)

    replay_parser = subparsers.add_parser(
        "replay", help="load the same inputs and replay idempotently"
    )
    replay_parser.add_argument("--db", required=True)
    replay_parser.add_argument("--events", required=True)
    replay_parser.add_argument("--quotas", required=True)
    replay_parser.add_argument("--policy", required=True)
    replay_parser.add_argument("--rounds", type=_positive, default=None)
    replay_parser.add_argument("--tenant", default=None)

    resume_parser = subparsers.add_parser(
        "resume", help="recover open intents and continue queued work"
    )
    resume_parser.add_argument("--db", required=True)
    resume_parser.add_argument("--rounds", type=_positive, default=None)
    resume_parser.add_argument("--tenant", default=None)
    resume_parser.add_argument(
        "--advance-clock-ms", type=_non_negative, default=0
    )
    resume_parser.add_argument(
        "--fail-key", action="append", default=[]
    )
    resume_parser.add_argument("--fail-count", type=_positive, default=1)

    status_parser = subparsers.add_parser(
        "status", help="show deterministic tenant/window ledger status"
    )
    status_parser.add_argument("--db", required=True)

    verify_parser = subparsers.add_parser(
        "verify", help="rebuild and verify ledger and event summaries"
    )
    verify_parser.add_argument("--db", required=True)
    verify_parser.add_argument("--expected", default=None)

    return parser


def _exit_code(exc: AuditError) -> int:
    if exc.code.startswith(("AUD-INPUT", "AUD-POLICY", "AUD-QUOTA", "AUD-EVENT-")):
        return 2
    if exc.code.startswith("AUD-LEDGER") or exc.code.startswith("AUD-STATE"):
        return 3
    return 4


def _fault_plan(keys: list[str], count: int) -> FaultPlan | None:
    if not keys:
        return None
    return FaultPlan(fail_counts={key: count for key in keys})


def _run_load(args: argparse.Namespace) -> dict[str, Any]:
    bundle = load_bundle(args.events, args.quotas, args.policy)
    store = AuditStore(args.db)
    return store.load_bundle(bundle, source="load")


def _run_write(args: argparse.Namespace, recovered_only: bool = False) -> dict[str, Any]:
    del recovered_only
    store = AuditStore(args.db)
    engine = AuditEngine(store, failure_hook=_fault_plan(
        getattr(args, "fail_key", []),
        getattr(args, "fail_count", 1),
    ))
    return engine.write(
        max_rounds=args.rounds,
        tenant=args.tenant,
        advance_clock_ms=args.advance_clock_ms,
        abandon_sequences=getattr(args, "abandon_seq", []),
    )


def _run_replay(args: argparse.Namespace) -> dict[str, Any]:
    bundle = load_bundle(args.events, args.quotas, args.policy)
    store = AuditStore(args.db)
    load_report = store.load_bundle(bundle, source="replay")
    engine = AuditEngine(store)
    write_report = engine.write(max_rounds=args.rounds, tenant=args.tenant)
    return {"ok": True, "load": load_report, "write": write_report}


def _run_status(args: argparse.Namespace) -> dict[str, Any]:
    return AuditStore(args.db).status()


def _run_verify(args: argparse.Namespace) -> dict[str, Any]:
    expected = None
    if args.expected:
        path = Path(args.expected)
        try:
            with path.open("r", encoding="utf-8") as handle:
                expected = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AuditError(
                "AUD-INPUT-INVALID-JSON",
                f"cannot read expected summary: {path}",
                details={"reason": str(exc)},
            ) from exc
    return AuditStore(args.db).verify(expected)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "load":
            result = _run_load(args)
        elif args.command == "write":
            result = _run_write(args)
        elif args.command == "replay":
            result = _run_replay(args)
        elif args.command == "resume":
            result = _run_write(args, recovered_only=True)
        elif args.command == "status":
            result = _run_status(args)
        elif args.command == "verify":
            result = _run_verify(args)
        else:
            parser.error(f"unsupported command: {args.command}")
            return 2
    except AuditError as exc:
        _print({"ok": False, "error": exc.to_dict()})
        return _exit_code(exc)
    except (OSError, ValueError, sqlite3.Error) as exc:
        _print(
            {
                "ok": False,
                "error": {
                    "code": "AUD-UNEXPECTED-ERROR",
                    "message": str(exc),
                    "type": type(exc).__name__,
                },
            }
        )
        return 4
    _print(result)
    if args.command == "verify" and not result.get("ok", False):
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
