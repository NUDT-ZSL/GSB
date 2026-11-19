"""Command-line interface for the deterministic alarm component."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from .canonical import load_json, stable_json_output
from .errors import AlarmError
from .parsing import parse_initial_windows, parse_topology
from .store import Store


def _add_global_options(parser: argparse.ArgumentParser, *, suppress_default: bool) -> None:
    parser.add_argument(
        "--db",
        default=argparse.SUPPRESS if suppress_default else "alarm_state.db",
        help="SQLite state file",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        default=argparse.SUPPRESS if suppress_default else False,
        help="Pretty-print JSON output",
    )


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alarm-dedup",
        description="Deterministic local alarm deduplication and root-cause correlation",
    )
    _add_global_options(parser, suppress_default=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="Validate topology and initialize SQLite state")
    _add_global_options(load, suppress_default=True)
    load.add_argument("--topology", required=True)
    load.add_argument("--window-ms", type=int, default=60000)

    ingest = subparsers.add_parser("ingest", help="Validate and ingest an alert event stream")
    _add_global_options(ingest, suppress_default=True)
    ingest.add_argument("--events", required=True)
    ingest.add_argument("--window-ms", type=int, default=None)

    patch = subparsers.add_parser("patch", help="Apply an incremental topology/window/weight patch")
    _add_global_options(patch, suppress_default=True)
    patch.add_argument("--patch", required=True)

    resume = subparsers.add_parser("resume", help="Replay durable pending operations")
    _add_global_options(resume, suppress_default=True)
    status = subparsers.add_parser("status", help="Show stable status JSON")
    _add_global_options(status, suppress_default=True)
    verify = subparsers.add_parser("verify", help="Rebuild in memory and verify persisted conclusions")
    _add_global_options(verify, suppress_default=True)
    return parser


def _run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    store = Store(args.db)
    if args.command == "load":
        if args.window_ms < 0:
            raise AlarmError(
                "CLUSTER_WINDOW_MISMATCH",
                "cluster window must be non-negative",
                window_ms=args.window_ms,
            )
        raw_topology = load_json(args.topology)
        topology = parse_topology(raw_topology)
        initial_windows = parse_initial_windows(raw_topology, topology)
        return store.initialize(topology, initial_windows, args.window_ms), 0
    if args.command == "ingest":
        raw_events = load_json(args.events)
        events, window_ms = store.prepare_ingest(raw_events, args.window_ms)
        return store.submit_ingest(events, window_ms), 0
    if args.command == "patch":
        raw_patch = load_json(args.patch)
        patch = store.prepare_patch(raw_patch)
        return store.submit_patch(patch), 0
    if args.command == "resume":
        result = store.resume()
        return result, 0 if result["ok"] else 1
    if args.command == "status":
        return store.status(), 0
    if args.command == "verify":
        result = store.verify()
        return result, 0 if result["ok"] else 1
    raise AlarmError("INTERNAL_ERROR", "Unsupported command", command=args.command)


def main(argv: list[str] | None = None) -> int:
    parser = _common_parser()
    args = parser.parse_args(argv)
    try:
        result, exit_code = _run(args)
    except AlarmError as exc:
        print(stable_json_output(exc.to_dict(), pretty=args.pretty))
        return 2
    except Exception as exc:
        payload = {
            "ok": False,
            "error_code": "INTERNAL_ERROR",
            "message": "Unexpected internal error",
            "details": {
                "exception_type": type(exc).__name__,
                "reason": str(exc),
            },
        }
        if os.environ.get("ALARM_DEDUP_DEBUG"):
            import traceback

            payload["details"]["traceback"] = traceback.format_exc()
        print(stable_json_output(payload, pretty=args.pretty))
        return 2
    print(stable_json_output(result, pretty=args.pretty))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
