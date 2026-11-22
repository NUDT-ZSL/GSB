"""Reproducible command-line diagnostics.

Subcommands: ``load``, ``aggregate``, ``query``, ``resume``, ``status``,
``verify`` (plus ``serve`` and ``generate`` for convenience).  ``status`` and
``verify`` always emit stable-sorted JSON.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Optional

from .engine import CRASH_AFTER_EVENT_INSERT, CRASH_AFTER_WINDOW_INSERT, Engine
from .errors import BusinessError
from .loader import read_bundle
from .loader import load_sample_bundle
from .models import parse_clock_config
from .query import QueryService
from .repository import Repository
from .storage import Workspace, dumps_canonical, transaction


def _print(payload: Any) -> None:
    print(dumps_canonical(payload) if not isinstance(payload, str) else payload)


def _workspace(args: argparse.Namespace) -> Workspace:
    workspace = Workspace(args.workspace)
    workspace.bootstrap()
    return workspace


def cmd_load(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    result = load_sample_bundle(
        workspace, Path(args.sample_dir), skip_manifest=args.skip_manifest
    )
    _print(result)
    return 0


def cmd_aggregate(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    _, _, clock_config = read_bundle(
        Path(args.sample_dir), skip_manifest=args.skip_manifest
    )
    if args.ticks:
        clock_config = parse_clock_config(
            {"ticks_ms": [int(t) for t in args.ticks.split(",") if t],
             "rollback_tolerance_ms": clock_config.rollback_tolerance_ms}
        )
    engine = Engine(workspace)
    crash_point = (
        CRASH_AFTER_EVENT_INSERT
        if args.crash_point == "event"
        else CRASH_AFTER_WINDOW_INSERT
    )
    result = engine.run(
        clock_config,
        crash_after_tick=args.crash_after_tick,
        crash_point=crash_point if args.crash_after_tick is not None else None,
    )
    _print(result)
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    conn = workspace.connect()
    try:
        with transaction(conn):
            service = QueryService(Repository(conn))
            if args.kind == "window":
                payload = service.query_window(
                    args.slo_name, args.window_no, verify=args.verify
                )
            else:
                payload = service.query_burn(
                    args.slo_name, at_window=args.at_window, verify=args.verify
                )
        _print(payload)
        return 0
    finally:
        conn.close()


def cmd_resume(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    clock_config = None
    if args.sample_dir:
        _, _, clock_config = read_bundle(
            Path(args.sample_dir), skip_manifest=args.skip_manifest
        )
    _print(Engine(workspace).resume(clock_config))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    conn = workspace.connect()
    try:
        with transaction(conn):
            payload = QueryService(Repository(conn)).status()
        _print(payload)
        return 0
    finally:
        conn.close()


def cmd_verify(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    conn = workspace.connect()
    try:
        with transaction(conn):
            payload = QueryService(Repository(conn)).verify_all()
        _print(payload)
        return 0
    finally:
        conn.close()


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import create_app

    app = create_app(args.workspace)
    app.run(host=args.host, port=args.port)
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    from scripts.generate_samples import generate

    digests = generate(Path(args.output_dir))
    _print({"output_dir": args.output_dir, "digests": digests})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slo-workbench", description=__doc__)
    parser.add_argument(
        "--workspace",
        default="./.slo_workbench",
        help="writable directory holding the SQLite database",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    load = sub.add_parser("load", help="validate and atomically load a sample bundle")
    load.add_argument("sample_dir")
    load.add_argument("--skip-manifest", action="store_true")
    load.set_defaults(func=cmd_load)

    aggregate = sub.add_parser("aggregate", help="advance the injected clock and close windows")
    aggregate.add_argument("sample_dir")
    aggregate.add_argument("--skip-manifest", action="store_true")
    aggregate.add_argument("--ticks", help="comma-separated override tick list")
    aggregate.add_argument("--crash-after-tick", type=int, default=None)
    aggregate.add_argument("--crash-point", choices=("event", "window"), default="window")
    aggregate.set_defaults(func=cmd_aggregate)

    query = sub.add_parser("query", help="query a cached window report or burn conclusion")
    query.add_argument("kind", choices=("window", "burn"))
    query.add_argument("slo_name")
    query.add_argument("--window-no", type=int)
    query.add_argument("--at-window", type=int)
    query.add_argument("--verify", action="store_true")
    query.set_defaults(func=cmd_query)

    resume = sub.add_parser("resume", help="recover missing windows after a hard kill")
    resume.add_argument("sample_dir", nargs="?")
    resume.add_argument("--skip-manifest", action="store_true")
    resume.set_defaults(func=cmd_resume)

    status = sub.add_parser("status", help="stable-sorted JSON status of every SLO")
    status.set_defaults(func=cmd_status)

    verify = sub.add_parser("verify", help="exact-recompute verification for all caches")
    verify.set_defaults(func=cmd_verify)

    serve = sub.add_parser("serve", help="run the Flask query interface")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5000)
    serve.set_defaults(func=cmd_serve)

    generate = sub.add_parser("generate", help="regenerate deterministic samples")
    generate.add_argument("output_dir", nargs="?", default="samples/default")
    generate.set_defaults(func=cmd_generate)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BusinessError as error:
        print(dumps_canonical(error.to_dict()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
