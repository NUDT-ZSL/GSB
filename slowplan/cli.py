from __future__ import annotations

import argparse
import sys
from typing import Any

from .engine import Engine
from .errors import BusinessError, E_IO, E_VERIFY_FAILED, exit_code_for
from .util import pretty_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slowplan",
        description="Deterministic local slow-query sampling and plan-profile engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="initialize state from a sample database and statistics snapshot")
    load.add_argument("--db", default="results/state.sqlite")
    load.add_argument("--sample-db", default="samples/sample_db.sqlite")
    load.add_argument("--stats", default="samples/stats_snapshot.json")
    load.add_argument("--window-ms", type=int, default=1000)
    load.add_argument("--clock-tolerance-ms", type=int, default=250)

    ingest = subparsers.add_parser("ingest", help="validate and atomically ingest a slow-query sample stream")
    ingest.add_argument("--db", default="results/state.sqlite")
    ingest.add_argument("--samples", required=True)
    ingest.add_argument("--now-ms", type=int, default=None)

    apply = subparsers.add_parser("apply", help="validate and apply a statistics refresh patch")
    apply.add_argument("--db", default="results/state.sqlite")
    apply.add_argument("--patch", required=True)
    apply.add_argument("--now-ms", type=int, default=None)

    profile = subparsers.add_parser("profile", help="refresh active-snapshot aggregates and plan profiles")
    profile.add_argument("--db", default="results/state.sqlite")
    profile.add_argument("--now-ms", type=int, default=None)
    profile.add_argument("--tolerance", type=float, default=5.0)

    resume = subparsers.add_parser("resume", help="resume journaled operations left by a forced termination")
    resume.add_argument("--db", default="results/state.sqlite")

    status = subparsers.add_parser("status", help="print stable JSON status")
    status.add_argument("--db", default="results/state.sqlite")

    verify = subparsers.add_parser("verify", help="verify persisted state and recomputed output")
    verify.add_argument("--db", default="results/state.sqlite")
    return parser


def execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    engine = Engine(args.db)
    if args.command == "load":
        result = engine.load(
            args.sample_db,
            args.stats,
            window_ms=args.window_ms,
            clock_tolerance_ms=args.clock_tolerance_ms,
        )
        return result, 0
    if args.command == "ingest":
        return engine.ingest(args.samples, now_ms=args.now_ms), 0
    if args.command == "apply":
        return engine.apply(args.patch, now_ms=args.now_ms), 0
    if args.command == "profile":
        result = engine.profile(now_ms=args.now_ms, tolerance=args.tolerance)
        if result.get("errors"):
            code = result["errors"][0]["code"]
            return result, exit_code_for(code)
        return result, 0
    if args.command == "resume":
        return engine.resume(), 0
    if args.command == "status":
        return engine.status(), 0
    if args.command == "verify":
        result = engine.verify()
        return result, 0 if result.get("ok") else exit_code_for(E_VERIFY_FAILED)
    raise BusinessError(E_IO, "unknown command", {"command": args.command})


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result, code = execute(args)
    except BusinessError as exc:
        print(pretty_json({"ok": False, "error": exc.as_dict()}), end="")
        return exit_code_for(exc.code)
    except OSError as exc:
        error = BusinessError(E_IO, "filesystem operation failed", {"reason": str(exc)})
        print(pretty_json({"ok": False, "error": error.as_dict()}), end="")
        return exit_code_for(error.code)
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        error = BusinessError(E_IO, "unexpected engine failure", {"reason": str(exc)})
        print(pretty_json({"ok": False, "error": error.as_dict()}), end="")
        return exit_code_for(error.code)
    print(pretty_json(result), end="")
    return code
