from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .config import load_bundle
from .errors import WorkbenchError
from .pipeline import run_workbench
from .reporting import write_outputs
from .signals import StopController, install_signal_handlers, restore_signal_handlers


OUTPUT_FILES = (
    "candidates.jsonl",
    "scored.jsonl",
    "retained.jsonl",
    "removed.jsonl",
    "failures.jsonl",
    "diagnostics.jsonl",
    "report.json",
)


def _fresh(output_dir: Path) -> None:
    state_dir = output_dir / "state"
    if state_dir.exists():
        shutil.rmtree(state_dir)
    for name in OUTPUT_FILES:
        path = output_dir / name
        if path.exists():
            path.unlink()


def _print_json(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), file=stream)


def _run(args: argparse.Namespace) -> int:
    input_dir = Path(args.input)
    output_dir = Path(args.output)
    bundle = load_bundle(input_dir)
    if args.fresh:
        _fresh(output_dir)
    controller = StopController()
    previous = install_signal_handlers(controller)
    try:
        outcome = run_workbench(
            bundle,
            output_dir,
            controller=controller,
            workers_override=args.workers,
            checkpoint_delay_ms=args.checkpoint_delay_ms,
            stop_after_checkpoints=args.stop_after_checkpoints,
        )
    finally:
        restore_signal_handlers(previous)

    if outcome.interrupted:
        state = outcome.state
        _print_json(
            {
                "status": "interrupted",
                "exit_code": 0,
                "signal": controller.signal_name,
                "snapshot_stage": state["snapshot_stage"],
                "cursor": state["cursor"],
                "snapshot_version": state["snapshot_version"],
                "checkpoints_this_run": outcome.checkpoints,
                "resumed": outcome.resumed,
            }
        )
        return 0

    report, _rows = write_outputs(bundle, outcome.state, output_dir)
    _print_json(
        {
            "status": "completed",
            "exit_code": 0,
            "report": str((output_dir / "report.json").resolve()),
            "canonical_report_fingerprint": report["canonical_report_fingerprint"],
            "summary": report["summary"],
            "resumed": outcome.resumed,
            "snapshot_version": report["snapshot"]["version"],
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="instruction-workbench",
        description="Deterministic instruction-sample construction and precision diagnostics.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="construct, score, diagnose, and deduplicate")
    run_parser.add_argument("--input", default="samples", help="input sample directory")
    run_parser.add_argument("--output", default="out", help="output directory")
    run_parser.add_argument(
        "--fresh", action="store_true", help="discard the previous snapshot and outputs"
    )
    run_parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="diagnostic-only worker override; normal runs use config.concurrency",
    )
    run_parser.add_argument(
        "--checkpoint-delay-ms",
        type=float,
        default=0.0,
        help="test-only delay after each atomic checkpoint",
    )
    run_parser.add_argument(
        "--stop-after-checkpoints",
        type=int,
        default=None,
        help="test-only graceful stop after N checkpoint writes",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return _run(args)
    except WorkbenchError as exc:
        _print_json({"status": "error", **exc.as_failure(type(exc).__name__)}, stream=sys.stderr)
        return 2
    except KeyboardInterrupt:
        _print_json(
            {
                "status": "interrupted",
                "exit_code": 0,
                "business_code": "E_SIGNAL_INTERRUPT",
                "message": "interrupt observed outside a checkpoint boundary",
            }
        )
        return 0
    parser.error("unknown command")
    return 2

