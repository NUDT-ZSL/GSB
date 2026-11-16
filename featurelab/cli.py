"""Command-line interface for the interactive feature engineering workbench."""

from __future__ import annotations

import argparse
import json
import queue
import signal
import sys
import threading
from typing import Any, Iterator, Optional

from .app import FeatureEngineeringApp
from .demo import DEFAULT_COMMANDS, write_demo_outputs
from .errors import FeatureLabError


def _stdin_lines(stop: threading.Event) -> Iterator[Optional[str]]:
    lines: queue.Queue[Optional[str]] = queue.Queue()

    def reader() -> None:
        try:
            while True:
                line = sys.stdin.readline()
                if line == "":
                    lines.put(None)
                    return
                lines.put(line.rstrip("\r\n"))
        except Exception:
            lines.put(None)

    thread = threading.Thread(target=reader, name="featurelab-stdin", daemon=True)
    thread.start()
    while not stop.is_set():
        try:
            item = lines.get(timeout=0.1)
        except queue.Empty:
            continue
        if item is None:
            return
        yield item


def _run_repl(args: argparse.Namespace) -> int:
    stop = threading.Event()

    def request_stop(signum: int, frame: Any) -> None:  # noqa: ARG001
        stop.set()

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, signal_name):
            try:
                signal.signal(getattr(signal, signal_name), request_stop)
            except (OSError, ValueError):
                pass
    try:
        app = FeatureEngineeringApp(
            args.config,
            serial=args.serial,
            cache_enabled=not args.no_cache,
            state_dir=args.state_dir,
            resume=not args.fresh,
        )
    except FeatureLabError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True))
        return 2
    for failure in app.failures:
        if failure.get("code") == "E_SNAPSHOT_CORRUPT":
            print(json.dumps(failure, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    if sys.stderr.isatty():
        print(
            f"FeatureLab ready; snapshot_version={app.snapshot_version}; "
            "type 'help' or 'quit'",
            file=sys.stderr,
        )
    try:
        for line in _stdin_lines(stop):
            if stop.is_set():
                break
            if not line.strip():
                continue
            response = app.handle_command(line)
            print(json.dumps(response, ensure_ascii=False, sort_keys=True))
            if line.strip().lower() in {"quit", "exit"}:
                break
    finally:
        app.close_with_exit_code()
        if args.report:
            app.write_report(args.report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="featurelab",
        description="Deterministic interactive feature engineering workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="run the fixed deterministic demo")
    demo.add_argument("--config", default="configs/experiment.yaml")
    demo.add_argument("--report", default="artifacts/demo-report.json")
    demo.add_argument("--equivalence-out", default="artifacts/equivalence.json")
    demo.add_argument("--verify", action="store_true")
    demo.add_argument("--serial", action="store_true")
    demo.add_argument("--no-cache", action="store_true")

    run = subparsers.add_parser("run", help="start the interactive REPL")
    run.add_argument("--config", default="configs/experiment.yaml")
    run.add_argument("--state-dir")
    run.add_argument("--report")
    run.add_argument("--serial", action="store_true")
    run.add_argument("--no-cache", action="store_true")
    run.add_argument("--fresh", action="store_true")

    once = subparsers.add_parser("once", help="execute one or more commands")
    once.add_argument("--config", default="configs/experiment.yaml")
    once.add_argument("--state-dir")
    once.add_argument("--report")
    once.add_argument("--serial", action="store_true")
    once.add_argument("--no-cache", action="store_true")
    once.add_argument("--command", action="append", dest="commands")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "demo":
        result = write_demo_outputs(
            args.config,
            args.report,
            verify=args.verify,
            equivalence_path=args.equivalence_out if args.verify else None,
            serial=args.serial,
            cache_enabled=not args.no_cache,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("verification", {}).get("equivalent", True) else 3
    if args.command == "run":
        return _run_repl(args)
    if args.command == "once":
        try:
            app = FeatureEngineeringApp(
                args.config,
                serial=args.serial,
                cache_enabled=not args.no_cache,
                state_dir=args.state_dir,
                resume=True,
            )
        except FeatureLabError as exc:
            print(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True))
            return 2
        for failure in app.failures:
            if failure.get("code") == "E_SNAPSHOT_CORRUPT":
                print(
                    json.dumps(failure, ensure_ascii=False, sort_keys=True),
                    file=sys.stderr,
                )
        try:
            for command in args.commands or ["evaluate"]:
                print(
                    json.dumps(
                        app.handle_command(command),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            app.wait_for_all()
            app.close_with_exit_code()
            if args.report:
                print(json.dumps({"report_path": str(app.write_report(args.report))}))
        finally:
            if not app._closed:
                app.close_with_exit_code()
        return 0
    parser.error("unknown command")
    return 2
