from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .canonical import pretty_json
from .errors import (
    E_INCOMPLETE_RUN,
    E_TRAJECTORY_MISMATCH,
    VerificationError,
    WorkbenchError,
)
from .runner import (
    preflight,
    resume_scenario,
    run_scenario,
    verify_run,
)
from .sample import write_sample_scenario
from .scenario import load_scenario
from .storage import SQLiteStore


def _add_crash_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--crash-before-commit",
        type=int,
        default=None,
        metavar="SEQ",
        help="diagnostic: hard-exit after writing SEQ inside its transaction",
    )
    parser.add_argument(
        "--crash-after-commit",
        type=int,
        default=None,
        metavar="SEQ",
        help="diagnostic: hard-exit immediately after committing SEQ",
    )
    parser.add_argument(
        "--pause-before-commit-ms",
        type=int,
        default=0,
        metavar="MS",
        help="diagnostic: pause inside the selected frame transaction",
    )
    parser.add_argument(
        "--pause-at-seq",
        type=int,
        default=None,
        metavar="SEQ",
        help="diagnostic: only pause at this frame sequence",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="h2diag",
        description="Deterministic local HTTP/2 multiplexing/recovery workbench",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate-sample", help="write a deterministic local sample scenario"
    )
    generate.add_argument("--out", required=True, help="scenario JSON output path")
    generate.add_argument("--seed", type=int, default=20261120)

    run = subparsers.add_parser("run", help="run a scenario from scratch")
    run.add_argument("--scenario", required=True)
    run.add_argument("--db", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--trace", default=None, help="write stable JSONL frame ledger")
    _add_crash_options(run)

    resume = subparsers.add_parser(
        "resume", help="resume from the last committed SQLite snapshot"
    )
    resume.add_argument("--scenario", required=True)
    resume.add_argument("--db", required=True)
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--trace", default=None, help="append stable JSONL frame ledger")
    _add_crash_options(resume)

    status = subparsers.add_parser(
        "status", help="print a stable JSON connection/stream snapshot"
    )
    status.add_argument("--db", required=True)
    status.add_argument("--run-id", required=True)

    verify = subparsers.add_parser(
        "verify", help="replay and verify persisted snapshots and digests"
    )
    verify.add_argument("--scenario", required=True)
    verify.add_argument("--db", required=True)
    verify.add_argument("--run-id", required=True)
    return parser


def _trace_open(path: str | None, mode: str) -> Any | None:
    if path is None:
        return None
    trace_path = Path(path)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    return open(trace_path, mode, encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate-sample":
            scenario = write_sample_scenario(args.out, args.seed)
            print(
                pretty_json(
                    {
                        "events": len(scenario["events"]),
                        "ok": True,
                        "output": str(Path(args.out)),
                        "scenario_id": scenario["scenario_id"],
                        "seed": args.seed,
                        "streams": len(scenario["streams"]),
                    }
                )
            )
            return 0

        if args.command == "run":
            scenario = load_scenario(args.scenario)
            preflight(scenario)
            trace = _trace_open(args.trace, "w")
            try:
                status = run_scenario(
                    scenario,
                    args.db,
                    args.run_id,
                    trace_handle=trace,
                    crash_before_commit=args.crash_before_commit,
                    crash_after_commit=args.crash_after_commit,
                    pause_before_commit_ms=args.pause_before_commit_ms,
                    pause_at_seq=args.pause_at_seq,
                )
            finally:
                if trace is not None:
                    trace.close()
            print(pretty_json(status))
            return 0

        if args.command == "resume":
            scenario = load_scenario(args.scenario)
            preflight(scenario)
            trace = _trace_open(args.trace, "a")
            try:
                status = resume_scenario(
                    scenario,
                    args.db,
                    args.run_id,
                    trace_handle=trace,
                    crash_before_commit=args.crash_before_commit,
                    crash_after_commit=args.crash_after_commit,
                    pause_before_commit_ms=args.pause_before_commit_ms,
                    pause_at_seq=args.pause_at_seq,
                )
            finally:
                if trace is not None:
                    trace.close()
            print(pretty_json(status))
            return 0

        if args.command == "status":
            store = SQLiteStore(args.db)
            try:
                store.open(create=False)
                print(pretty_json(store.status(args.run_id)))
            finally:
                store.close()
            return 0

        if args.command == "verify":
            scenario = load_scenario(args.scenario)
            report = verify_run(scenario, args.db, args.run_id)
            print(pretty_json(report))
            return 0

        parser.error("unknown command")
        return 2
    except (VerificationError, WorkbenchError) as exc:
        payload = {"error": exc.to_dict(), "ok": False}
        print(pretty_json(payload), file=sys.stderr)
        if isinstance(exc, VerificationError) or exc.code in {
            E_INCOMPLETE_RUN,
            E_TRAJECTORY_MISMATCH,
        }:
            return 4
        return 3
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(
            pretty_json(
                {
                    "error": {
                        "code": "E_IO",
                        "details": {},
                        "message": str(exc),
                    },
                    "ok": False,
                }
            ),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

