from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any

from .errors import E_TRACE_DIVERGENCE, EngineError
from .pipeline import execute_commands, status_payload, verify_payload


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )


def _emit_error(error: EngineError) -> None:
    payload = {"status": "error", **error.to_dict()}
    sys.stderr.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quicsim",
        description="Deterministic local QUIC recovery and congestion-control simulator",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run a scene from a fresh or replaced session")
    run.add_argument("--scene", required=True, help="path to the JSON scene")
    run.add_argument("--db", required=True, help="SQLite state database")
    run.add_argument(
        "--commit-every",
        type=int,
        default=1,
        help="number of events committed atomically per version (default: 1)",
    )
    run.add_argument(
        "--max-events",
        type=int,
        default=None,
        help="stop after committing at most this many events",
    )
    run.add_argument(
        "--crash-after",
        type=int,
        default=None,
        help="inject an abrupt process exit after N committed batches",
    )
    run.add_argument(
        "--restart",
        action="store_true",
        help="start a new session even when a committed run exists",
    )

    resume = subparsers.add_parser("resume", help="resume after the committed watermark")
    resume.add_argument("--scene", required=True, help="path to the JSON scene")
    resume.add_argument("--db", required=True, help="SQLite state database")
    resume.add_argument("--commit-every", type=int, default=1)
    resume.add_argument(
        "--max-events",
        type=int,
        default=None,
        help="stop after this many additional events",
    )
    resume.add_argument("--crash-after", type=int, default=None)

    status = subparsers.add_parser("status", help="read stable status JSON")
    status.add_argument("--db", required=True)
    status.add_argument("--scene-id", default=None)

    verify = subparsers.add_parser(
        "verify", help="verify persisted dual-path traces and independent replay"
    )
    verify.add_argument("--db", required=True)
    verify.add_argument("--scene-id", default=None)
    verify.add_argument(
        "--scene",
        default=None,
        help="optional scene file whose hash must match the persisted scene",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            payload = execute_commands(
                args.scene,
                args.db,
                resume=False,
                commit_every=args.commit_every,
                max_events=args.max_events,
                crash_after=args.crash_after,
                restart=args.restart,
            )
            _emit(payload)
            return 3 if payload.get("failure_code") else 0
        if args.command == "resume":
            payload = execute_commands(
                args.scene,
                args.db,
                resume=True,
                commit_every=args.commit_every,
                max_events=args.max_events,
                crash_after=args.crash_after,
            )
            _emit(payload)
            return 3 if payload.get("failure_code") else 0
        if args.command == "status":
            payload = status_payload(args.db, args.scene_id)
            _emit(payload)
            return 0
        if args.command == "verify":
            payload = verify_payload(
                args.db, args.scene_id, scene_path=args.scene
            )
            _emit(payload)
            return 0 if payload.get("ok") else 1
        parser.error("unknown command")
    except EngineError as error:
        _emit_error(error)
        if error.code == E_TRACE_DIVERGENCE:
            return 4
        return 2
    except sqlite3.Error as error:
        _emit_error(EngineError("E_DB", str(error)))
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

