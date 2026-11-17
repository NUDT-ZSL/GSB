"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from typing import Sequence

from . import __version__
from .errors import BusinessError
from .processor import TerminationController, run_processing
from .task import load_task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hdr-color",
        description="Deterministic local SDR/HLG/PQ conversion and HDR mapping",
    )
    parser.add_argument(
        "task",
        nargs="?",
        help="path to hdr-color-task-v1 JSON",
    )
    parser.add_argument(
        "--task",
        dest="task_option",
        help="path to hdr-color-task-v1 JSON (alternative to positional)",
    )
    parser.add_argument(
        "--test-chunk-delay-ms",
        type=int,
        default=0,
        help="test-only delay at each chunk boundary; not part of task data",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    task_path = args.task_option or args.task
    if not task_path:
        parser.error("a task JSON path is required")
    if args.test_chunk_delay_ms < 0:
        parser.error("--test-chunk-delay-ms must be non-negative")
    delay_ms = args.test_chunk_delay_ms
    if delay_ms == 0:
        try:
            delay_ms = max(0, int(os.environ.get("HDR_COLOR_TEST_CHUNK_DELAY_MS", "0")))
        except ValueError:
            delay_ms = 0

    termination = TerminationController(event=threading.Event())
    _install_signal_handlers(termination)
    try:
        loaded = load_task(task_path)
        outcome = run_processing(
            loaded.config,
            termination,
            test_chunk_delay_seconds=delay_ms / 1000.0,
        )
        _print_json(outcome)
        if outcome.get("status") == "interrupted":
            return int(outcome.get("exit_code") or 143)
        return 0
    except BusinessError as exc:
        _print_json(exc.as_dict())
        return exc.exit_code
    except KeyboardInterrupt:
        _print_json(
            {
                "status": "interrupted",
                "signal": "SIGINT",
                "exit_code": 130,
            }
        )
        return 130
    except Exception as exc:
        _print_json(
            BusinessError(
                "E_INTERNAL",
                "unexpected internal failure",
                {"error_type": type(exc).__name__, "error": str(exc)},
            ).as_dict()
        )
        return 2


def _install_signal_handlers(termination: TerminationController) -> None:
    def handler(signum: int, _frame: object) -> None:
        termination.request(signum)

    for name in ("SIGINT", "SIGTERM"):
        candidate = getattr(signal, name, None)
        if candidate is None:
            continue
        try:
            signal.signal(candidate, handler)
        except (OSError, ValueError):
            pass


def _print_json(payload: object) -> None:
    json.dump(
        payload,
        sys.stdout,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )
    sys.stdout.write("\n")
    sys.stdout.flush()
