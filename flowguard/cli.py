"""Command line interface for Flowguard."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

from .engine import Adjudicator
from .errors import ErrorCode, FlowGuardError
from .models import (
    load_json_file,
    parse_event_row,
    parse_events,
    parse_policy,
    parse_sample,
    parse_topology,
)
from .store import Store, meta_get
from .util import sorted_json_dumps
from .verifier import Verifier, compare_sample_digests


DEFAULT_DB = "data/flowguard.db"
DEFAULT_TOPOLOGY = "samples/topology.json"
DEFAULT_EVENTS = "samples/events.json"
DEFAULT_POLICY = "samples/policy.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flowguard",
        description="Deterministic local rate limiting, circuit breaking and degradation adjudicator",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate and load a fixed sample")
    load_parser.add_argument("--db", default=DEFAULT_DB)
    load_parser.add_argument("--topology", default=DEFAULT_TOPOLOGY)
    load_parser.add_argument("--events", default=DEFAULT_EVENTS)
    load_parser.add_argument("--policy", default=DEFAULT_POLICY)
    load_parser.add_argument("--reset", action="store_true")
    _add_output(load_parser)

    run_parser = subparsers.add_parser("run", help="process all pending events")
    run_parser.add_argument("--db", default=DEFAULT_DB)
    run_parser.add_argument(
        "--events",
        help="optionally ingest another event JSON file before running",
    )
    run_parser.add_argument(
        "--lenient-events",
        dest="lenient_events",
        action="store_true",
        default=True,
        help="with --events, record per-event errors and continue (default)",
    )
    run_parser.add_argument(
        "--strict-events",
        dest="lenient_events",
        action="store_false",
        help="with --events, reject the whole extra event file on any error",
    )
    run_parser.add_argument(
        "--crash-after-stage",
        type=int,
        default=None,
        help="test hook: hard-exit after staging the Nth event",
    )
    _add_output(run_parser)

    resume_parser = subparsers.add_parser(
        "resume",
        help="recover staged/loaded events and continue processing",
    )
    resume_parser.add_argument("--db", default=DEFAULT_DB)
    _add_output(resume_parser)

    status_parser = subparsers.add_parser("status", help="show durable governance status")
    status_parser.add_argument("--db", default=DEFAULT_DB)
    status_parser.add_argument("--service")
    _add_output(status_parser)

    explain_parser = subparsers.add_parser(
        "explain",
        help="show the complete decision chain for one event",
    )
    explain_parser.add_argument("--db", default=DEFAULT_DB)
    explain_parser.add_argument("--seq", type=int, required=True)
    _add_output(explain_parser)

    verify_parser = subparsers.add_parser(
        "verify",
        help="verify invariants and deterministic replay",
    )
    verify_parser.add_argument("--db", default=DEFAULT_DB)
    verify_parser.add_argument("--topology")
    verify_parser.add_argument("--events")
    verify_parser.add_argument("--policy")
    verify_parser.add_argument("--expected-decision-digest")
    verify_parser.add_argument("--no-replay", action="store_true")
    _add_output(verify_parser)
    return parser


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", help="write stable sorted JSON to this local path")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "load":
            result = _command_load(args)
        elif args.command == "run":
            result = _command_run(args)
        elif args.command == "resume":
            result = _command_resume(args)
        elif args.command == "status":
            result = _command_status(args)
        elif args.command == "explain":
            result = _command_explain(args)
        elif args.command == "verify":
            result = _command_verify(args)
        else:
            raise FlowGuardError(
                ErrorCode.NOT_FOUND,
                "unknown command",
                {"command": args.command},
                exit_code=2,
            )
        _emit(result, args.output)
        if result.get("ok") is False:
            return 3
        return 0
    except FlowGuardError as exc:
        _emit(
            {
                "ok": False,
                "command": args.command,
                "error": exc.as_dict(),
            },
            getattr(args, "output", None),
        )
        return exc.exit_code
    except sqlite3.OperationalError as exc:
        error = FlowGuardError(
            ErrorCode.STATE_INCONSISTENT,
            "SQLite operation failed",
            {"reason": str(exc)},
            exit_code=4,
        )
        _emit(
            {
                "ok": False,
                "command": args.command,
                "error": error.as_dict(),
            },
            getattr(args, "output", None),
        )
        return error.exit_code


def _command_load(args: argparse.Namespace) -> dict[str, Any]:
    sample = parse_sample(
        load_json_file(args.topology),
        load_json_file(args.events),
        load_json_file(args.policy),
    )
    adjudicator = Adjudicator(args.db)
    result = adjudicator.load_sample(sample, reset=args.reset)
    return {
        "ok": True,
        "command": "load",
        "database": args.db,
        **result,
    }


def _command_run(args: argparse.Namespace) -> dict[str, Any]:
    adjudicator = Adjudicator(args.db)
    ingest_errors: list[dict[str, Any]] = []
    if args.events:
        ingest_errors = _ingest_events_file(
            adjudicator,
            args.events,
            lenient=args.lenient_events,
        )
    crash_after = args.crash_after_stage
    env_crash = os.environ.get("FLOWGUARD_CRASH_AFTER_STAGE")
    if crash_after is None and env_crash:
        try:
            crash_after = int(env_crash)
        except ValueError:
            crash_after = None
    report = adjudicator.run(crash_after_stage=crash_after)
    report["command"] = "run"
    report["ingest_errors"] = ingest_errors
    if ingest_errors:
        report["ok"] = False
    return report


def _command_resume(args: argparse.Namespace) -> dict[str, Any]:
    adjudicator = Adjudicator(args.db)
    if not adjudicator.store.is_loaded():
        raise FlowGuardError(
            ErrorCode.NOT_FOUND,
            "database has not been loaded",
            {"database": args.db},
        )
    store = Store(args.db)
    conn = store.connect()
    try:
        staged = int(
            conn.execute(
                "SELECT COUNT(*) FROM pending_jobs WHERE status = 'pending'"
            ).fetchone()[0]
        )
        loaded = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM events e
                LEFT JOIN decisions d ON d.seq = e.seq
                WHERE d.seq IS NULL
                """
            ).fetchone()[0]
        )
    finally:
        conn.close()
    report = adjudicator.run()
    report["command"] = "resume"
    report["recovered_staged_events"] = staged
    report["recovered_loaded_events"] = max(0, loaded - staged)
    return report


def _command_status(args: argparse.Namespace) -> dict[str, Any]:
    return Adjudicator(args.db).status(service=args.service)


def _command_explain(args: argparse.Namespace) -> dict[str, Any]:
    return Adjudicator(args.db).explain(args.seq)


def _command_verify(args: argparse.Namespace) -> dict[str, Any]:
    expected_digests = None
    provided = [args.topology, args.events, args.policy]
    if any(provided):
        if not all(provided):
            raise FlowGuardError(
                ErrorCode.SAMPLE_DIGEST_MISMATCH,
                "topology, events and policy must be provided together",
                {"provided": provided},
                exit_code=2,
            )
        expected_digests = compare_sample_digests(
            args.topology,
            args.events,
            args.policy,
        )
    verifier = Verifier(args.db)
    return verifier.verify(
        expected_digests=expected_digests,
        expected_decision_digest=args.expected_decision_digest,
        replay=not args.no_replay,
    )


def _ingest_events_file(
    adjudicator: Adjudicator,
    events_path: str,
    *,
    lenient: bool,
) -> list[dict[str, Any]]:
    raw = load_json_file(events_path)
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict) and isinstance(raw.get("events"), list):
        rows = raw["events"]
    else:
        raise FlowGuardError(
            ErrorCode.EVENT_SCHEMA_INVALID,
            "event file must contain an events array",
        )
    conn = adjudicator.store.connect()
    try:
        topology_raw = json.loads(meta_get(conn, "topology_json") or "{}")
        policy_raw = json.loads(meta_get(conn, "policy_json") or "{}")
    finally:
        conn.close()
    if not topology_raw:
        raise FlowGuardError(
            ErrorCode.NOT_FOUND,
            "load topology before ingesting events",
        )
    topology = parse_topology(topology_raw)
    parse_policy(policy_raw, topology)
    if not lenient:
        events = parse_events({"events": rows}, topology)
        adjudicator.ingest_events(events)
        return []

    known = set(topology.service_map())
    errors: list[dict[str, Any]] = []
    previous_seq = 0
    for index, row in enumerate(rows):
        try:
            event = parse_event_row(row, index, known)
            if event.seq <= previous_seq:
                raise FlowGuardError(
                    ErrorCode.EVENT_SEQ_NOT_INCREASING,
                    "event.seq must be strictly increasing",
                    {
                        "index": index,
                        "previous_seq": previous_seq,
                        "seq": event.seq,
                    },
                    exit_code=2,
                )
            previous_seq = event.seq
            adjudicator.ingest_events((event,))
        except FlowGuardError as exc:
            errors.append(
                {
                    "index": index,
                    "event": row if isinstance(row, dict) else repr(row),
                    "error": exc.as_dict(),
                }
            )
    return errors


def _emit(payload: Any, output: str | None) -> None:
    rendered = sorted_json_dumps(payload)
    if output:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n", encoding="utf-8")
    else:
        sys.stdout.write(rendered + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
