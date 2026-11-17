"""Command-line entry point."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

from .report import build_fatal_report, write_report
from .runner import run_pipeline


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic local CTR reranking pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="run or resume a pipeline snapshot")
    run_parser.add_argument("--config", required=True)
    run_parser.add_argument("--candidates", required=True)
    run_parser.add_argument("--params", required=True)
    run_parser.add_argument("--state-dir", required=True)
    run_parser.add_argument("--report", required=True)
    run_parser.add_argument("--concurrency", type=int, default=None)
    run_parser.add_argument("--exit-after", type=int, default=None, dest="exit_after")
    run_parser.add_argument("--crash-after", type=int, default=None, dest="crash_after")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.error("unknown command")

    stop_event = threading.Event()

    def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
        stop_event.set()

    previous_int = signal.signal(signal.SIGINT, _handle_signal)
    previous_term = None
    if hasattr(signal, "SIGTERM"):
        previous_term = signal.signal(signal.SIGTERM, _handle_signal)
    try:
        outcome = run_pipeline(
            config_path=args.config,
            candidates_path=args.candidates,
            params_path=args.params,
            state_dir=args.state_dir,
            report_path=args.report,
            concurrency_override=args.concurrency,
            exit_after_requests=args.exit_after,
            crash_after_requests=args.crash_after,
            stop_event=stop_event,
        )
        return outcome.exit_code
    except Exception as exc:
        report = build_fatal_report(
            status="fatal",
            code="E_INTERNAL",
            stage="runner",
            exception_type=type(exc).__name__,
            message=str(exc),
        )
        try:
            write_report(Path(args.report), report)
        finally:
            return 3
    finally:
        signal.signal(signal.SIGINT, previous_int)
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    sys.exit(main())
