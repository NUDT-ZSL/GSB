from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path
from typing import Any

from .errors import AlertLabError
from .service import AlertLabService
from .util import pretty_json, read_json


class CrashController:
    def __init__(self, expression: str | None):
        self.stage: str | None = None
        self.target = 1
        self.seen = 0
        if expression:
            stage, _, count = expression.partition(":")
            self.stage = stage
            self.target = int(count or "1")

    def __call__(self, stage: str, object_id: str) -> None:
        if stage != self.stage:
            return
        self.seen += 1
        if self.seen >= self.target:
            sys.stderr.write(
                f"ALERTLAB_CRASH stage={stage} object={object_id} count={self.seen}\n"
            )
            sys.stderr.flush()
            os._exit(73)


def _service(args: argparse.Namespace) -> AlertLabService:
    database = args.database or (Path(args.workspace) / "data" / "alertlab.sqlite3")
    return AlertLabService(args.workspace, database_path=database)


def _print(value: Any) -> None:
    print(pretty_json(value))


def command_load(args: argparse.Namespace) -> int:
    service = _service(args)
    result = service.load(
        v1_path=args.v1,
        v2_path=args.v2,
        topology_path=args.topology,
        events_path=args.events,
        logical_clock_ms=args.clock,
        include_v1=not args.no_v1,
        include_v2=not args.no_v2,
        crash_hook=CrashController(args.crash_at),
    )
    _print(result)
    return 0


def command_patch(args: argparse.Namespace) -> int:
    service = _service(args)
    path = Path(args.patch)
    if not path.is_absolute():
        path = Path(args.workspace) / path
    result = service.patch(
        read_json(path), crash_hook=CrashController(args.crash_at)
    )
    _print(result)
    return 0


def command_status(args: argparse.Namespace) -> int:
    _print(_service(args).status())
    return 0


def command_verify(args: argparse.Namespace) -> int:
    result = _service(args).verify()
    _print(result)
    return 0 if result["ok"] else 1


def command_recover(args: argparse.Namespace) -> int:
    _print(_service(args).recover())
    return 0


def command_reset(args: argparse.Namespace) -> int:
    service = _service(args)
    service.store.reset()
    _print(service.status())
    return 0


def _check_loopback_port(port: int) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise SystemExit(
            f"port 127.0.0.1:{port} is unavailable: {exc}; choose another --port"
        )
    finally:
        probe.close()


def command_serve(args: argparse.Namespace) -> int:
    _check_loopback_port(args.port)
    from .web import create_app

    app = create_app(args.workspace, database_path=args.database)
    app.run(
        host="127.0.0.1",
        port=args.port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alertlab",
        description="Deterministic local SRE alert reduction demo",
    )
    parser.add_argument(
        "--workspace",
        default=str(Path.cwd()),
        help="workspace root containing samples/ and data/",
    )
    parser.add_argument("--database", default=None, help="working SQLite path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="load rule snapshots and event stream")
    load.add_argument("--v1", default="samples/v1/rules_v1.json")
    load.add_argument("--v2", default="samples/v2/index.json")
    load.add_argument("--topology", default="samples/topology.json")
    load.add_argument("--events", default="samples/events/alerts.json")
    load.add_argument("--clock", type=int, default=None)
    load.add_argument("--no-v1", action="store_true")
    load.add_argument("--no-v2", action="store_true")
    load.add_argument(
        "--crash-at",
        default=None,
        help="forced process exit hook, e.g. cluster_after_commit:2",
    )
    load.set_defaults(func=command_load)

    patch = subparsers.add_parser("patch", help="apply an incremental rule patch")
    patch.add_argument("patch")
    patch.add_argument("--crash-at", default=None)
    patch.set_defaults(func=command_patch)

    status = subparsers.add_parser("status", help="print stable current status")
    status.set_defaults(func=command_status)

    verify = subparsers.add_parser("verify", help="verify persisted conclusions")
    verify.set_defaults(func=command_verify)

    recover = subparsers.add_parser("recover", help="resume incomplete transactions")
    recover.set_defaults(func=command_recover)

    reset = subparsers.add_parser("reset", help="clear the working database")
    reset.set_defaults(func=command_reset)

    serve = subparsers.add_parser("serve", help="run Flask on 127.0.0.1")
    serve.add_argument("--port", type=int, default=5000)
    serve.set_defaults(func=command_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except AlertLabError as error:
        print(pretty_json(error.to_dict()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
