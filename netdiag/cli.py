from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .demo import run_demo
from .engine import DiagnosticEngine
from .errors import BusinessError
from .mock_service import LocalMockService, load_mock_config
from .scenario import load_scenario
from .store import Store


def stable_json(value: Any) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
        )
        + "\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netdiag",
        description=(
            "Deterministic local loopback packet-loss and network fault diagnosis"
        ),
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("NETDIAG_DB", "netdiag.db"),
        help="SQLite state database (default: %(default)s)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate and load a JSON scenario")
    load_parser.add_argument("scenario")

    probe_parser = subparsers.add_parser(
        "probe", help="probe one pending round or all pending rounds"
    )
    probe_parser.add_argument("--round", type=int)
    probe_parser.add_argument(
        "--target",
        action="append",
        dest="targets",
        help="restrict to a target; may be repeated",
    )
    probe_parser.add_argument(
        "--all",
        action="store_true",
        help="continue through all pending current-config rounds",
    )
    probe_parser.add_argument(
        "--mock-config",
        help="start a local mock HTTP service for the duration of this command",
    )
    probe_parser.add_argument(
        "--mock-port",
        type=int,
        default=18080,
        help="port used with --mock-config",
    )

    diagnose_parser = subparsers.add_parser(
        "diagnose", help="recompute and compare diagnoses from committed samples"
    )
    diagnose_parser.add_argument(
        "--target", action="append", dest="targets", help="restrict to a target"
    )

    resume_parser = subparsers.add_parser(
        "resume", help="recover prepared attempts and finish pending rounds"
    )
    resume_parser.add_argument(
        "--mock-config",
        help="start a local mock HTTP service for the duration of this command",
    )
    resume_parser.add_argument(
        "--mock-port",
        type=int,
        default=18080,
        help="port used with --mock-config",
    )

    subparsers.add_parser("status", help="print stable sorted persisted status")
    subparsers.add_parser("verify", help="verify persisted state and deterministic output")

    serve_parser = subparsers.add_parser(
        "serve-mock", help="run the local loopback fault-injection HTTP service"
    )
    serve_parser.add_argument("--config", default="samples/mock_service.json")
    serve_parser.add_argument("--port", type=int, default=18080)

    demo_parser = subparsers.add_parser(
        "demo", help="run the fixed sample twice and compare byte-identical reports"
    )
    demo_parser.add_argument("--workdir", default="artifacts/demo")
    demo_parser.add_argument("--scenario", default="samples/scenario_demo.json")
    demo_parser.add_argument("--mock-config", default="samples/mock_service.json")
    demo_parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="loopback port for the demo service; 0 selects a free port",
    )
    return parser


def _emit(value: Any) -> None:
    sys.stdout.write(stable_json(value))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "load":
            scenario = load_scenario(args.scenario)
            with Store(args.db) as store:
                engine = DiagnosticEngine(store)
                result = engine.load_scenario(scenario)
            _emit(result)
            return 0
        if args.command == "probe":
            with Store(args.db) as store:
                engine = DiagnosticEngine(store)
                with _optional_mock_service(args):
                    result = engine.run(
                        round_no=args.round,
                        target_names=args.targets,
                        all_rounds=args.all,
                    )
            _emit(result)
            return 0
        if args.command == "diagnose":
            with Store(args.db) as store:
                result = DiagnosticEngine(store).diagnose(args.targets)
            _emit(result)
            return 0
        if args.command == "resume":
            with Store(args.db) as store:
                engine = DiagnosticEngine(store)
                with _optional_mock_service(args):
                    result = engine.run(recover_first=True, all_rounds=True)
            _emit(result)
            return 0
        if args.command == "status":
            with Store(args.db) as store:
                result = store.status()
            _emit(result)
            return 0
        if args.command == "verify":
            with Store(args.db) as store:
                result = DiagnosticEngine(store).verify()
            _emit(result)
            return 0 if result.get("ok") else 1
        if args.command == "serve-mock":
            service = LocalMockService(
                load_mock_config(args.config), port=args.port
            ).start()
            _emit({"ok": True, "base_url": service.base_url})
            try:
                while True:
                    import time

                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            finally:
                service.stop()
            return 0
        if args.command == "demo":
            result = run_demo(
                workdir=Path(args.workdir),
                scenario_path=Path(args.scenario),
                mock_config_path=Path(args.mock_config),
                port=args.port,
            )
            _emit(result)
            return 0 if result.get("ok") else 1
        parser.error(f"unknown command: {args.command}")
    except BusinessError as exc:
        sys.stdout.write(stable_json(exc.as_dict()))
        return exc.exit_code
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        return 130


class _optional_mock_service:
    def __init__(self, args: argparse.Namespace) -> None:
        self.config_path = getattr(args, "mock_config", None)
        self.port = getattr(args, "mock_port", 18080)
        self.service: LocalMockService | None = None

    def __enter__(self) -> "_optional_mock_service":
        if self.config_path:
            self.service = LocalMockService(
                load_mock_config(self.config_path), port=self.port
            ).start()
        return self

    def __exit__(self, *_args: object) -> None:
        if self.service is not None:
            self.service.stop()
