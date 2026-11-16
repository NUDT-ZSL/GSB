"""Command line entry point for the feature cross / reduction workbench."""
from __future__ import annotations

import argparse
import os
import sys

from .atomicio import atomic_write_json
from .config import load_config
from .errors import BenchError
from .pipeline import run_pipeline
from .report import write_report
from .snapshot import ShutdownFlag


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="featbench",
        description="Feature cross combination and reduction experiment workbench")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run the experiment pipeline")
    run_p.add_argument("config", help="path to the YAML experiment config")
    run_p.add_argument("--workers", type=int, default=None,
                       help="override config workers")
    run_p.add_argument("--report-only-fingerprint", action="store_true",
                       help="print config fingerprint and exit")

    val_p = sub.add_parser("validate", help="validate a config and print fingerprint")
    val_p.add_argument("config")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except BenchError as exc:
        failure = {"failures": [exc.to_failure(stage="config")]}
        os.makedirs(".bench_work", exist_ok=True)
        atomic_write_json(os.path.join(".bench_work", "featbench_invalid_config.json"), failure)
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2

    if args.command == "validate" or getattr(
            args, "report_only_fingerprint", False):
        print(config.config_fingerprint)
        return 0

    if getattr(args, "workers", None) is not None:
        config.workers = args.workers
        config.raw["workers"] = args.workers

    shutdown = ShutdownFlag()
    shutdown.install()
    try:
        report = run_pipeline(config, shutdown)
    except BenchError as exc:
        failure = {"failures": [exc.to_failure(stage="pipeline")]}
        os.makedirs(".bench_work", exist_ok=True)
        atomic_write_json(os.path.join(".bench_work", "featbench_failure.json"), failure)
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2
    finally:
        shutdown.restore()

    if shutdown.requested:
        print("shutdown requested; stage snapshots persisted", file=sys.stderr)
        return 0
    path = write_report(config, report)
    print(f"report written: {path}")
    print(f"config fingerprint: {config.config_fingerprint}")
    print(f"report fingerprint: {report['report_fingerprint']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
