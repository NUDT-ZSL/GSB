from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from .engine import LayoutEngine
from .errors import ConfigError, EngineError
from .interactive import InteractiveSession, run_repl
from .util import atomic_write_json
from .version import ENGINE_VERSION


def _write_report(path: Path, report: dict[str, Any]) -> None:
    atomic_write_json(path, report)


def _configuration_error_report(error: EngineError, config_path: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "engine_version": ENGINE_VERSION,
        "run": {
            "status": "config_error",
            "exit_code": 2,
            "config_path": str(config_path),
            "global_version": None,
        },
        "summary": {
            "pages_scanned": 0,
            "pages_processed": 0,
            "pages_succeeded": 0,
            "pages_failed": 0,
            "pending_pages": [],
        },
        "pages": [],
        "reuse_recompute": [],
        "failures": [error.as_failure()],
        "business_codes": [error.code],
        "interaction": {
            "latency_budget_ms": None,
            "commands": [],
            "all_within_budget": None,
        },
    }


def _print_summary(report: dict[str, Any]) -> None:
    summary = {
        "status": report.get("run", {}).get("status"),
        "exit_code": report.get("run", {}).get("exit_code"),
        "business_codes": report.get("business_codes", []),
        "pages_processed": report.get("summary", {}).get("pages_processed"),
        "pages_failed": report.get("summary", {}).get("pages_failed"),
        "reading_order_entries": report.get("summary", {}).get("reading_order_entries"),
        "truth_validation": report.get("summary", {}).get("truth_validation"),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="layout-engine",
        description="Deterministic synthetic-layout reading-order analysis engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run full or incremental analysis")
    run_parser.add_argument("--images", type=Path, required=True)
    run_parser.add_argument("--config", type=Path, required=True)
    run_parser.add_argument("--metadata", type=Path, required=True)
    run_parser.add_argument("--snapshot", type=Path, required=True)
    run_parser.add_argument("--report", type=Path, required=True)
    run_parser.add_argument("--truth", type=Path, default=None)
    run_parser.add_argument(
        "--allow-config-change",
        action="store_true",
        help="allow selective cache invalidation after editing analysis parameters",
    )
    run_parser.add_argument(
        "--full-rebuild",
        action="store_true",
        help="ignore stage artifacts and recompute every page",
    )

    interactive_parser = subparsers.add_parser(
        "interactive", help="edit config/pages and run incremental analysis per command"
    )
    interactive_parser.add_argument("--images", type=Path, required=True)
    interactive_parser.add_argument("--config", type=Path, required=True)
    interactive_parser.add_argument("--metadata", type=Path, required=True)
    interactive_parser.add_argument("--snapshot", type=Path, required=True)
    interactive_parser.add_argument("--report", type=Path, required=True)
    interactive_parser.add_argument("--truth", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        stop_event = threading.Event()
        try:
            engine = LayoutEngine(
                args.images,
                args.config,
                args.metadata,
                args.snapshot,
                truth_path=args.truth,
                full_rebuild=args.full_rebuild,
                allow_config_change=args.allow_config_change,
                stop_event=stop_event,
            )
        except ConfigError as exc:
            report = _configuration_error_report(exc, args.config)
            _write_report(args.report, report)
            _print_summary(report)
            return 2
        def request_stop(signum, frame) -> None:  # noqa: ANN001, ARG001
            stop_event.set()

        previous_handlers: dict[int, Any] = {}
        for signal_name in ("SIGINT", "SIGTERM"):
            signal_value = getattr(signal, signal_name, None)
            if signal_value is None:
                continue
            try:
                previous_handlers[signal_value] = signal.getsignal(signal_value)
                signal.signal(signal_value, request_stop)
            except (OSError, ValueError):
                pass
        try:
            result = engine.run()
        except EngineError as exc:
            report = _configuration_error_report(exc, args.config)
            _write_report(args.report, report)
            _print_summary(report)
            return 2
        finally:
            for signal_value, previous in previous_handlers.items():
                try:
                    signal.signal(signal_value, previous)
                except (OSError, ValueError):
                    pass
        _write_report(args.report, result.report)
        _print_summary(result.report)
        return result.exit_code
    if args.command == "interactive":
        try:
            session = InteractiveSession(
                images_dir=args.images,
                config_path=args.config,
                metadata_path=args.metadata,
                snapshot_dir=args.snapshot,
                report_path=args.report,
                truth_path=args.truth,
            )
        except ConfigError as exc:
            report = _configuration_error_report(exc, args.config)
            _write_report(args.report, report)
            _print_summary(report)
            return 2
        return run_repl(session)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
