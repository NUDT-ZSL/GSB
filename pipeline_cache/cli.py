"""Command-line interface for the local artifact cache engine."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .engine import Engine
from .errors import E_INTERNAL, BusinessError


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir",
        default=".ci-cache",
        help="state directory (default: .ci-cache)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="pretty-print deterministic JSON",
    )


def _add_local_common(parser: argparse.ArgumentParser) -> None:
    """Allow global options after the subcommand as well as before it."""
    parser.add_argument("--state-dir", default=argparse.SUPPRESS)
    parser.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cicache",
        description="Local deterministic CI/CD artifact tiering and capacity engine",
    )
    _add_common(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="validate and load a deterministic snapshot")
    _add_local_common(load)
    load.add_argument("--tasks", required=True)
    load.add_argument("--pipelines", required=True)
    load.add_argument("--config", required=True)
    load.add_argument(
        "--reset",
        action="store_true",
        help="explicitly replace an existing local state directory",
    )

    run = subparsers.add_parser("run", help="commit one pipeline run")
    _add_local_common(run)
    run.add_argument("--pipeline", required=True)
    run.add_argument("--run-seq", type=int)
    run.add_argument("--at-ms", type=int)

    judge = subparsers.add_parser(
        "judge", help="evaluate or preview hit/tier decisions without mutation"
    )
    _add_local_common(judge)
    judge.add_argument("--pipeline", required=True)
    judge.add_argument("--run-seq", type=int)
    judge.add_argument("--at-ms", type=int)

    evict = subparsers.add_parser(
        "evict", help="enforce tier capacity and reclaim unreferenced objects"
    )
    _add_local_common(evict)
    evict.add_argument("--at-ms", type=int)
    evict.add_argument("--pipeline")
    evict.add_argument("--run-seq", type=int)
    evict.add_argument(
        "--candidate",
        action="append",
        help="force a task candidate (repeatable); useful for diagnostics",
    )

    resume = subparsers.add_parser("resume", help="finish durable recovery work")
    _add_local_common(resume)
    status = subparsers.add_parser("status", help="show persisted cache state")
    _add_local_common(status)

    verify = subparsers.add_parser("verify", help="verify database, CAS, and accounts")
    _add_local_common(verify)
    verify.add_argument(
        "--skip-hash",
        action="store_true",
        help="check file presence and size only",
    )
    return parser


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    engine = Engine(args.state_dir)
    if args.command == "load":
        return engine.load_paths(
            args.tasks, args.pipelines, args.config, reset=args.reset
        )
    if args.command == "run":
        return engine.run(
            args.pipeline, run_seq=args.run_seq, at_ms=args.at_ms
        )
    if args.command == "judge":
        return engine.judge(
            args.pipeline, run_seq=args.run_seq, at_ms=args.at_ms
        )
    if args.command == "evict":
        return engine.evict(
            at_ms=args.at_ms,
            pipeline=args.pipeline,
            run_seq=args.run_seq,
            candidates=args.candidate,
        )
    if args.command == "resume":
        return engine.resume()
    if args.command == "status":
        return engine.status()
    if args.command == "verify":
        return engine.verify(full_hash=not args.skip_hash)
    raise BusinessError(E_INTERNAL, f"unsupported command {args.command}")


def _emit(payload: dict[str, Any], *, pretty: bool) -> None:
    if pretty:
        json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
    else:
        json.dump(
            payload,
            sys.stdout,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    sys.stdout.write("\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = _dispatch(args)
        _emit(payload, pretty=args.pretty)
        return 0 if payload.get("ok") else 1
    except BusinessError as error:
        _emit(error.as_dict(), pretty=args.pretty)
        return 1
    except sqlite3.Error as error:
        wrapped = BusinessError(E_INTERNAL, f"sqlite failure: {error}")
        _emit(wrapped.as_dict(), pretty=args.pretty)
        return 1
    except BrokenPipeError:
        return 0
    except OSError as error:
        wrapped = BusinessError(E_INTERNAL, f"I/O failure: {error}", path=str(Path.cwd()))
        _emit(wrapped.as_dict(), pretty=args.pretty)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
