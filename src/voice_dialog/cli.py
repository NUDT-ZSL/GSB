"""Command-line interface for deterministic generation and pipeline runs."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from .config import ConfigError, load_config, load_injections
from .errors import BusinessError
from .pipeline import Pipeline
from .reporting import attach_report_fingerprint
from .synthetic import generate_demo
from .util import atomic_write_json


def _error_report(error: Exception) -> dict[str, Any]:
    if isinstance(error, BusinessError):
        detail = error.to_failure()
    else:
        detail = {
            "code": "E_INTERNAL",
            "phase": "startup",
            "filename": "",
            "turn_id": "",
            "exception_type": type(error).__name__,
            "message": str(error),
            "injection_id": "",
        }
    report = {
        "report_version": "1.0",
        "status": "failed",
        "error": detail,
        "segments": [],
        "dialog_turns": [],
        "failures": [detail],
        "cache": {},
        "snapshots": {"versions": {}, "committed": {}, "errors": []},
    }
    return attach_report_fingerprint(report)


def _install_signal_handlers(stop_event: threading.Event) -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        stop_event.set()

    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    for signum in signals:
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


def _summary(report: dict[str, Any], report_path: Path) -> dict[str, Any]:
    diagnostics = report.get("diagnostics", {})
    return {
        "report": str(report_path),
        "status": report.get("status"),
        "report_fingerprint": report.get("report_fingerprint"),
        "scale_tier": diagnostics.get("scale_tier", report.get("scale_decision", {}).get("metrics", {}).get("tier")),
        "effective_classifier": diagnostics.get("effective_classifier"),
        "effective_fusion_window": diagnostics.get("effective_fusion_window"),
        "segments": diagnostics.get("segment_count", len(report.get("segments", []))),
        "fused_turns": diagnostics.get("fused_turn_count", len(report.get("dialog_turns", []))),
        "failures": diagnostics.get("failure_count", len(report.get("failures", []))),
        "injection_hits": diagnostics.get("injection_hit_count", len(report.get("injections", {}).get("hits", []))),
    }


def _run_command(args: argparse.Namespace) -> int:
    report_path = args.out
    try:
        config = load_config(args.config)
        injections = load_injections(args.injections)
    except Exception as exc:
        report = _error_report(exc)
        atomic_write_json(report_path, report)
        print(json.dumps(_summary(report, report_path), ensure_ascii=False, sort_keys=True))
        return 2
    stop_event = threading.Event()
    previous_handlers = _install_signal_handlers(stop_event)
    try:
        pipeline = Pipeline(
            samples_dir=args.samples,
            script_path=args.script,
            config=config,
            injections=injections,
            state_dir=args.state,
            mode=args.mode,
            stop_event=stop_event,
        )
        try:
            report = pipeline.run()
        except Exception as exc:
            report = _error_report(exc)
            atomic_write_json(report_path, report)
            print(json.dumps(_summary(report, report_path), ensure_ascii=False, sort_keys=True))
            return 2
    finally:
        _restore_signal_handlers(previous_handlers)
    atomic_write_json(report_path, report)
    print(json.dumps(_summary(report, report_path), ensure_ascii=False, sort_keys=True))
    if report.get("status") in {"completed", "interrupted"}:
        return 0
    return 2


def _generate_command(args: argparse.Namespace) -> int:
    manifest = generate_demo(
        args.out,
        seed=args.seed,
        segments=args.segments,
        turns=args.turns,
        duration=args.duration,
        include_anomalies=not args.no_anomalies,
        write_default_configs=not args.no_configs,
    )
    print(json.dumps({"out": str(args.out), **manifest}, ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voice-dialog",
        description="Deterministic local acoustic emotion and dialogue-understanding pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run feature/scoring/fusion pipeline")
    run.add_argument("--samples", required=True, type=Path, help="directory containing WAV files")
    run.add_argument("--script", required=True, type=Path, help="dialogue script JSON")
    run.add_argument("--config", required=True, type=Path, help="configuration JSON")
    run.add_argument("--injections", type=Path, default=None, help="anomaly injection JSON")
    run.add_argument("--state", required=True, type=Path, help="cache and snapshot directory")
    run.add_argument("--out", required=True, type=Path, help="diagnostic report JSON path")
    run.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    run.set_defaults(func=_run_command)

    generate = subparsers.add_parser("generate", help="generate fixed-seed WAV/dialogue samples")
    generate.add_argument("--out", required=True, type=Path)
    generate.add_argument("--seed", type=int, default=20261117)
    generate.add_argument("--segments", type=int, default=76)
    generate.add_argument("--turns", type=int, default=18)
    generate.add_argument("--duration", type=float, default=0.82)
    generate.add_argument("--no-anomalies", action="store_true")
    generate.add_argument("--no-configs", action="store_true")
    generate.set_defaults(func=_generate_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
