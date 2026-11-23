from __future__ import annotations

import argparse
import ipaddress
import signal
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

from .api import create_app
from .config import load_config, parse_profile_update
from .diagnostics import verify_state
from .echo_service import EchoEndpoint, EchoHub, EchoService
from .engine import ForwardEngine, install_test_kill_hook
from .errors import BusinessError, fail
from .storage import Storage
from .util import load_json_strict, stable_json


def _print(value: Any) -> None:
    print(stable_json(value), flush=True)


def _require_database(storage: Storage) -> None:
    if not storage.has_schema():
        fail(
            "E_CONFIG_NOT_LOADED",
            "database is not initialized; run the load command first",
            details={"db_path": str(storage.db_path)},
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tunnelwb",
        description="Deterministic local tunnel/VPN forwarding diagnostic workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="validate and atomically install a JSON sample")
    load.add_argument("--config", required=True)
    load.add_argument("--db", required=True)
    load.add_argument("--lock-dir")

    echo = subparsers.add_parser("echo", help="start local UDP/TCP loopback echo targets")
    echo.add_argument("--config")
    echo.add_argument("--transport", choices=["udp", "tcp", "both"])
    echo.add_argument("--host", default="127.0.0.1")
    echo.add_argument("--port", type=int)

    forward = subparsers.add_parser("forward", help="create a run and advance forwarding rounds")
    forward.add_argument("--config", required=True)
    forward.add_argument("--db", required=True)
    forward.add_argument("--tunnel", required=True)
    forward.add_argument("--payload-index", type=int, default=0)
    forward.add_argument("--rounds", type=int, default=1)
    forward.add_argument("--transport", choices=["udp", "tcp"])
    forward.add_argument("--salt", type=int, default=0)
    forward.add_argument("--lock-dir")

    resume = subparsers.add_parser("resume", help="continue the latest resumable run for a tunnel")
    resume.add_argument("--config", required=True)
    resume.add_argument("--db", required=True)
    resume.add_argument("--tunnel", required=True)
    resume.add_argument("--rounds", type=int, default=1)
    resume.add_argument("--transport", choices=["udp", "tcp"])
    resume.add_argument("--lock-dir")

    status = subparsers.add_parser("status", help="emit stable-sorted persisted diagnostics")
    status.add_argument("--db", required=True)
    status.add_argument("--lock-dir")

    verify = subparsers.add_parser("verify", help="recompute and verify persisted state")
    verify.add_argument("--config")
    verify.add_argument("--db", required=True)
    verify.add_argument("--lock-dir")

    abort = subparsers.add_parser("abort", help="request an active abort")
    abort.add_argument("--db", required=True)
    abort.add_argument("--run-id")
    abort.add_argument("--tunnel")
    abort.add_argument("--clock-ms", type=int, default=0)
    abort.add_argument("--lock-dir")

    profile = subparsers.add_parser("profile-replace", help="atomically replace a link profile")
    profile.add_argument("--db", required=True)
    profile.add_argument("--tunnel", required=True)
    profile.add_argument("--profile", required=True)
    profile.add_argument("--lock-dir")

    serve = subparsers.add_parser("serve", help="start the Flask diagnostic API")
    serve.add_argument("--config", required=True)
    serve.add_argument("--db", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--lock-dir")
    return parser


def _storage(args: argparse.Namespace) -> Storage:
    return Storage(args.db, lock_dir=getattr(args, "lock_dir", None))


def _validate_rounds(rounds: int) -> int:
    if rounds < 0:
        fail("E_ROUNDS_INVALID", "rounds must be non-negative")
    return rounds


def _run_engine_command(args: argparse.Namespace, *, resume: bool) -> dict[str, Any]:
    config = load_config(args.config)
    storage = _storage(args)
    _require_database(storage)
    abort_event = threading.Event()
    previous_handler = signal.getsignal(signal.SIGINT)

    def on_sigint(signum, frame) -> None:
        del signum, frame
        abort_event.set()

    signal.signal(signal.SIGINT, on_sigint)
    try:
        engine = ForwardEngine(
            storage,
            config,
            abort_event=abort_event,
            kill_hook=install_test_kill_hook(),
        )
        if resume:
            return engine.resume(
                tunnel=args.tunnel,
                rounds=_validate_rounds(args.rounds),
                transport_override=args.transport,
            )
        return engine.run_new(
            tunnel=args.tunnel,
            payload_index=args.payload_index,
            rounds=_validate_rounds(args.rounds),
            transport_override=args.transport,
            salt=args.salt,
        )
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def _echo(args: argparse.Namespace) -> dict[str, Any]:
    if args.config:
        config = load_config(args.config)
        service = EchoService.from_config(config)
        description = [
            {
                "transport": endpoint.transport,
                "host": endpoint.host,
                "port": endpoint.port,
            }
            for endpoint in service.endpoints
        ]
    else:
        if args.port is None or not args.transport:
            fail("E_ECHO_ARGUMENTS", "ad-hoc echo requires --transport and --port")
        try:
            host = str(ipaddress.ip_address(args.host))
        except ValueError:
            fail("E_LISTEN_ADDR_INVALID", "echo host must be a numeric IP address")
        if not ipaddress.ip_address(host).is_loopback:
            fail("E_LISTEN_NOT_LOOPBACK", "echo host must be loopback")
        if not 1 <= args.port <= 65535:
            fail("E_LISTEN_ADDR_INVALID", "echo port must be in 1..65535")
        transports = ["udp", "tcp"] if args.transport == "both" else [args.transport]
        endpoints = [EchoEndpoint(transport, host, args.port) for transport in transports]
        service = EchoService(endpoints, EchoHub())
        description = [
            {"transport": endpoint.transport, "host": endpoint.host, "port": endpoint.port}
            for endpoint in endpoints
        ]
    service.start()
    _print({"ok": True, "status": "ECHO_READY", "endpoints": description})
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
    return {"ok": True, "status": "ECHO_STOPPED", "endpoints": description}


def _serve(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    app = create_app(db_path=args.db, config=config, config_path=None)
    app.extensions["tunnel_storage"].lock_dir = Path(
        args.lock_dir or str(Path(args.db).parent / "locks")
    )
    if not 1 <= args.port <= 65535:
        fail("E_LISTEN_ADDR_INVALID", "API port must be in 1..65535")
    try:
        host = str(ipaddress.ip_address(args.host))
    except ValueError:
        fail("E_LISTEN_ADDR_INVALID", "API host must be a numeric IP address")
    if not ipaddress.ip_address(host).is_loopback:
        fail("E_LISTEN_NOT_LOOPBACK", "API host must be loopback")
    app.run(host=host, port=args.port, debug=False, use_reloader=False)
    return {"ok": True, "status": "API_STOPPED"}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "load":
            config = load_config(args.config)
            storage = _storage(args)
            result = storage.install_config(config)
            _print(
                {
                    "ok": True,
                    "command": "load",
                    "config_digest": config.content_digest,
                    "installed": result["installed"],
                }
            )
            return 0
        if args.command == "echo":
            result = _echo(args)
            _print(result)
            return 0
        if args.command in ("forward", "resume"):
            result = _run_engine_command(args, resume=args.command == "resume")
            _print(result)
            return 0 if result["ok"] else 3
        if args.command == "status":
            storage = _storage(args)
            _require_database(storage)
            _print({"ok": True, "command": "status", **storage.status()})
            return 0
        if args.command == "verify":
            storage = _storage(args)
            _require_database(storage)
            config = load_config(args.config) if args.config else None
            result = verify_state(storage, config)
            _print(result)
            return 0 if result["ok"] else 4
        if args.command == "abort":
            if not args.run_id and not args.tunnel:
                fail("E_REQUEST_BODY", "abort requires --run-id or --tunnel")
            storage = _storage(args)
            _require_database(storage)
            run = (
                storage.get_run(args.run_id)
                if args.run_id
                else storage.latest_resumable_run(args.tunnel)
            )
            clock = args.clock_ms or run["logical_clock_ms"]
            result = storage.abort_run(run["run_id"], clock)
            _print({"ok": True, "command": "abort", "run": result})
            return 0
        if args.command == "profile-replace":
            raw = load_json_strict(args.profile)
            profile = parse_profile_update(raw, args.tunnel)
            result = _storage(args).replace_profile(profile)
            _print({"ok": True, "command": "profile-replace", "profile": result})
            return 0
        if args.command == "serve":
            result = _serve(args)
            _print(result)
            return 0
        parser.error("unknown command")
        return 2
    except BusinessError as exc:
        _print({"ok": False, "error": exc.as_dict()})
        return 2
    except (OSError, sqlite3.Error) as exc:
        _print(
            {
                "ok": False,
                "error": {
                    "code": "E_STORAGE_ERROR",
                    "message": str(exc),
                    "details": {},
                },
            }
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())

