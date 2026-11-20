from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Sequence

from . import __version__
from .engine import (
    TailSamplingEngine,
    parse_inputs,
    parse_late_inputs,
    reset_database_files,
)
from .errors import BusinessError
from .util import load_json, pretty_json


DEFAULT_DB = os.environ.get("TAILSAMPLE_DB", "artifacts/tailsample.db")


def emit(value: Any, stream: Any = None) -> None:
    target = sys.stdout if stream is None else stream
    target.write(pretty_json(value))
    target.write("\n")


def add_db_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"SQLite database path (default: {DEFAULT_DB})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tailsample",
        description="Deterministic local tail-sampling decision engine",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser(
        "load", help="validate and atomically stage topology, policies and events"
    )
    add_db_argument(load_parser)
    load_parser.add_argument("--topology", required=True)
    load_parser.add_argument("--policies", required=True)
    load_parser.add_argument("--events", required=True)
    load_parser.add_argument("--window-ms", type=int, default=900)
    load_parser.add_argument("--quota-per-second", type=int, default=2)
    load_parser.add_argument("--clock-tolerance-ms", type=int, default=10)
    load_parser.add_argument(
        "--reset",
        action="store_true",
        help="remove the local database, WAL and lock files before loading",
    )

    sample_parser = subparsers.add_parser(
        "sample", help="advance the logical clock and commit due decisions"
    )
    add_db_argument(sample_parser)
    sample_parser.add_argument("--advance-to", type=int, default=None)
    sample_parser.add_argument(
        "--late-spans",
        default=None,
        help="JSON file containing spans that arrived after decisions",
    )

    query_parser = subparsers.add_parser(
        "query", help="emit the stable trace, decision, evidence and late convergence view"
    )
    add_db_argument(query_parser)
    query_parser.add_argument("--trace-id", required=True)

    policy_parser = subparsers.add_parser(
        "policy", help="list, inspect or activate policy versions"
    )
    policy_sub = policy_parser.add_subparsers(dest="policy_command", required=True)
    policy_list = policy_sub.add_parser("list")
    add_db_argument(policy_list)
    policy_show = policy_sub.add_parser("show")
    add_db_argument(policy_show)
    policy_show.add_argument("--version", required=True)
    policy_activate = policy_sub.add_parser("activate")
    add_db_argument(policy_activate)
    policy_activate.add_argument("--version", required=True)
    policy_activate.add_argument("--at-ms", type=int, required=True)

    resume_parser = subparsers.add_parser(
        "resume", help="replay unprocessed events and finish due windows"
    )
    add_db_argument(resume_parser)

    status_parser = subparsers.add_parser(
        "status", help="show waiting pool, quota, late and recovery state"
    )
    add_db_argument(status_parser)

    verify_parser = subparsers.add_parser(
        "verify", help="verify persisted chains, quota ledger and decision fingerprints"
    )
    add_db_argument(verify_parser)

    return parser


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.command == "load":
        topology, policies, events = parse_inputs(
            args.topology, args.policies, args.events
        )
        if args.reset:
            reset_database_files(args.db)
        with TailSamplingEngine(args.db) as engine:
            result = engine.load_dataset(
                topology,
                policies,
                events,
                window_ms=args.window_ms,
                quota_per_second=args.quota_per_second,
                clock_tolerance_ms=args.clock_tolerance_ms,
            )
        return result, 0

    if args.command == "sample":
        late_events: list[dict[str, Any]] = []
        if args.late_spans:
            late_events = parse_late_inputs(args.late_spans, ())
        with TailSamplingEngine(args.db) as engine:
            result = engine.sample(
                advance_to_ms=args.advance_to,
                late_events=late_events,
            )
        return result, 3 if result.get("errors") else 0

    if args.command == "query":
        with TailSamplingEngine(args.db) as engine:
            return engine.query_trace(args.trace_id), 0

    if args.command == "policy":
        with TailSamplingEngine(args.db) as engine:
            if args.policy_command == "list":
                return engine.policy_list(), 0
            if args.policy_command == "show":
                return engine.policy_show(args.version), 0
            if args.policy_command == "activate":
                return engine.policy_activate(args.version, args.at_ms), 0
        raise AssertionError("unreachable policy command")

    if args.command == "resume":
        with TailSamplingEngine(args.db) as engine:
            return engine.resume(), 0

    if args.command == "status":
        with TailSamplingEngine(args.db) as engine:
            return engine.status(), 0

    if args.command == "verify":
        with TailSamplingEngine(args.db) as engine:
            result = engine.verify()
        return result, 0 if result["ok"] else 1

    raise AssertionError(f"unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result, exit_code = run(args)
    except BusinessError as exc:
        emit(exc.as_dict(), sys.stderr)
        return exc.exit_code
    emit(result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

