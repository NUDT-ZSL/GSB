"""Command line entry points for generation, validation and execution."""

from __future__ import annotations

import argparse
import json
import signal
import threading
from pathlib import Path
from typing import Any

from .data_generator import generate_dataset
from .errors import ConfigMismatchError
from .injection import load_injections
from .loader import load_corpus
from .pipeline import PipelineRunner, run_pipeline
from .report import config_mismatch_report
from .resources import load_resources
from .snapshot import SnapshotStore
from .utils import atomic_write_json


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))


def _install_signal_handlers(stop_event: threading.Event) -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def handler(signum: int, frame: Any) -> None:
        stop_event.set()

    candidates = [getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None), getattr(signal, "SIGBREAK", None)]
    for signum in candidates:
        if signum is None:
            continue
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handler)
        except (ValueError, OSError):
            pass
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):
            pass


def run_command(args: argparse.Namespace) -> int:
    corpus_path = Path(args.corpus)
    resources_dir = Path(args.resources)
    config_path = Path(args.config) if args.config else resources_dir / "config.json"
    injection_path = Path(args.inject) if args.inject else None
    state_dir = Path(args.state_dir)
    report_path = Path(args.report)
    stop_event = threading.Event()
    previous = _install_signal_handlers(stop_event)
    try:
        result = run_pipeline(
            corpus_path,
            resources_dir,
            config_path=config_path,
            injection_path=injection_path,
            state_dir=state_dir,
            workers=args.workers,
            fresh=args.fresh,
            interrupt_after=args.interrupt_after,
            stop_event=stop_event,
        )
        atomic_write_json(report_path, result.report)
        _print_json(result.report)
        return 0
    except ConfigMismatchError as exc:
        version = 0
        try:
            version = SnapshotStore(state_dir).latest_version()
        except OSError:
            version = 0
        report = config_mismatch_report(
            error=exc,
            corpus_path=str(corpus_path),
            config_path=str(config_path),
            injection_path=str(injection_path) if injection_path is not None else None,
            snapshot_version=version,
        )
        atomic_write_json(report_path, report)
        _print_json(report)
        return 2
    except KeyboardInterrupt:
        stop_event.set()
        # A second path for platforms that deliver KeyboardInterrupt before the
        # custom handler is invoked.  Existing snapshots remain the only source
        # of truth for a later recovery.
        report = {
            "status": "interrupted",
            "complete": False,
            "interrupted": True,
            "message": "interrupted before the runner could publish its report",
        }
        _print_json(report)
        return 0
    finally:
        _restore_signal_handlers(previous)


def validate_command(args: argparse.Namespace) -> int:
    resources = load_resources(args.resources, args.config)
    load_result = load_corpus(
        args.corpus,
        intent_names=resources.intent_names,
        allowed_types=resources.entity_types,
    )
    injections = load_injections(args.inject) if args.inject else None
    report = {
        "resources": resources.as_summary(),
        "corpus": load_result.as_report(),
        "injections": injections.as_report() if injections is not None else [],
    }
    _print_json(report)
    return 0


def generate_command(args: argparse.Namespace) -> int:
    manifest = generate_dataset(args.out, args.seed)
    _print_json(manifest)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="intent-engine", description="Deterministic Chinese intent and slot engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run the pipeline and write a JSON report")
    run_parser.add_argument("--corpus", default="data/corpus.jsonl")
    run_parser.add_argument("--resources", default="data/resources")
    run_parser.add_argument("--config", default=None)
    run_parser.add_argument("--inject", default=None)
    run_parser.add_argument("--state-dir", default="state")
    run_parser.add_argument("--report", default="reports/report.json")
    run_parser.add_argument("--workers", type=int, default=None)
    run_parser.add_argument("--fresh", action="store_true", help="do not resume a snapshot, but reuse the stage cache")
    run_parser.add_argument("--interrupt-after", type=int, default=None, help="stage a deterministic graceful stop after N new samples")
    run_parser.set_defaults(func=run_command)

    validate_parser = subparsers.add_parser("validate", help="validate and summarize local inputs without running")
    validate_parser.add_argument("--corpus", default="data/corpus.jsonl")
    validate_parser.add_argument("--resources", default="data/resources")
    validate_parser.add_argument("--config", default=None)
    validate_parser.add_argument("--inject", default=None)
    validate_parser.set_defaults(func=validate_command)

    generate_parser = subparsers.add_parser("generate", help="regenerate the deterministic local data set")
    generate_parser.add_argument("--out", default="data")
    generate_parser.add_argument("--seed", type=int, default=42)
    generate_parser.set_defaults(func=generate_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

