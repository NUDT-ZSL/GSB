"""Command-line entry point for the offline video pipeline."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Sequence

from .atomic_io import atomic_write_json
from .config import load_detection_config, load_matching_config
from .errors import ConfigError
from .pipeline import Pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-pipeline",
        description="Offline deterministic sliding-window detection and trajectory pipeline",
    )
    parser.add_argument("--input", required=True, help="directory containing local video files")
    parser.add_argument("--model", required=True, help="detection model parameter JSON")
    parser.add_argument("--matching", required=True, help="matching and budget JSON")
    parser.add_argument("--output", default="artifacts/report.json", help="final JSON report path")
    parser.add_argument("--state-dir", default="artifacts/state", help="snapshot directory")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="ignore existing snapshots and start a fresh run",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    stop_event = threading.Event()
    previous_handlers: dict[int, Any] = {}

    def handle_signal(signum: int, frame: Any) -> None:
        del signum, frame
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signum] = signal.signal(signum, handle_signal)
        except (ValueError, OSError):
            pass

    try:
        try:
            detection_config = load_detection_config(args.model)
            matching_config = load_matching_config(args.matching)
        except ConfigError as exc:
            _write_fatal_report(
                Path(args.output),
                code="E_CONFIG_INVALID",
                stage="config",
                exception_type=type(exc).__name__,
                message=str(exc),
            )
            print(f"configuration invalid: {exc}", file=sys.stderr)
            return 2

        try:
            pipeline = Pipeline(
                args.input,
                detection_config,
                matching_config,
                args.output,
                args.state_dir,
                resume=not args.reset,
                stop_event=stop_event,
            )
            result = pipeline.run()
        except FileNotFoundError as exc:
            _write_fatal_report(
                Path(args.output),
                code="E_INPUT_INVALID",
                stage="scan",
                exception_type=type(exc).__name__,
                message=str(exc),
            )
            print(str(exc), file=sys.stderr)
            return 2
        except Exception as exc:
            _write_fatal_report(
                Path(args.output),
                code="E_UNEXPECTED_ERROR",
                stage="pipeline",
                exception_type=type(exc).__name__,
                message=str(exc),
            )
            print(f"pipeline failed: {exc}", file=sys.stderr)
            return 1

        summary = {
            "status": result.report["status"],
            "report": str(result.report_path),
            "tracks": len(result.report["tracks"]),
            "failures": len(result.report["failures"]),
            "frames_processed": result.report["budget"]["frames_processed"],
            "result_hash": result.report["result_hash"],
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass


def _write_fatal_report(
    output_path: Path,
    *,
    code: str,
    stage: str,
    exception_type: str,
    message: str,
) -> None:
    report = {
        "schema_version": 1,
        "status": "failed",
        "config_hash": None,
        "result_hash": None,
        "exit_reason": code,
        "tracks": [],
        "routes": [],
        "failures": [
            {
                "route": "*",
                "frame_index": None,
                "stage": stage,
                "code": code,
                "exception_type": exception_type,
                "message": message,
            }
        ],
        "budget": {
            "max_workers": None,
            "total_frame_budget": None,
            "allocated_frames": 0,
            "frames_processed": 0,
            "remaining_frame_budget": None,
            "peak_workers": 0,
            "elapsed_seconds": 0.0,
        },
        "recovery": {
            "resume_enabled": False,
            "detection_snapshot_loaded": False,
            "matching_snapshot_loaded": False,
        },
    }
    atomic_write_json(output_path, report)
