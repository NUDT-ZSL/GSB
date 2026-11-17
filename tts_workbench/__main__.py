from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from .engine import Workbench


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tts-workbench",
        description="Deterministic offline TTS acoustic renderer with resumable editing",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    render = subparsers.add_parser("render", help="render or resume a sample directory")
    render.add_argument("--input", required=True, type=Path, help="input sample directory")
    render.add_argument("--work", required=True, type=Path, help="writable work directory")
    render.add_argument(
        "--commands",
        action="append",
        default=[],
        type=Path,
        help="additional edit command JSONL file (repeatable)",
    )
    render.add_argument(
        "--reset",
        action="store_true",
        help="ignore an existing snapshot and rebuild from the input labels",
    )
    render.add_argument(
        "--crash-after-checkpoints",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    render.add_argument("--quiet", action="store_true", help="suppress the stdout summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stop_event = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    workbench = Workbench(
        input_dir=args.input,
        work_dir=args.work,
        stop_event=stop_event,
        crash_after_checkpoints=args.crash_after_checkpoints,
    )
    result = workbench.run(extra_command_paths=args.commands, reset=args.reset)
    if not args.quiet:
        summary = {
            "status": result.status,
            "exit_code": result.exit_code,
            "report": str(result.report_path),
            "business_codes": result.report.get("business_codes", []),
            "failures": len(result.report.get("failures", [])),
            "outputs": [
                item.get("outputs", {}).get("wav")
                for item in result.report.get("utterances", [])
                if item.get("outputs")
            ],
            "recovery_point": result.report.get("recovery_point"),
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
