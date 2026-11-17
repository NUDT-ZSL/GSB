"""Command-line entry point for deterministic voiceprint diagnostics."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from .canonical import write_json_atomic
from .config import ConfigError
from .pipeline import PipelineInputError, run_pipeline
from .snapshots import ConfigMismatchError
from .synthesize import generate_demo


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voiceprint-diag")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run the complete diagnostic pipeline")
    run.add_argument("--samples", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--pairs", required=True)
    run.add_argument("--anomalies", required=True)
    run.add_argument("--output", required=True)
    run.add_argument(
        "--resume",
        action="store_true",
        help="require an existing snapshot with the same global version/config/injection hash",
    )
    run.add_argument("--quiet", action="store_true")
    demo = subparsers.add_parser("generate-demo", help="generate fixed-seed WAV samples and manifests")
    demo.add_argument("--output", required=True)
    demo.add_argument("--seed", type=int, default=20261117)
    return parser


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    deterministic = report["deterministic_results"]
    return {
        "status": report["status"],
        "result_fingerprint": report["result_fingerprint"],
        "summary": deterministic["summary"],
        "stage_timings_ms": report["stage_timings_ms"],
        "incremental": {
            "mode": report["incremental"]["snapshot_mode"],
            "reused_count": report["incremental"]["reused_count"],
            "recomputed_count": report["incremental"]["recomputed_count"],
            "invalidated_count": report["incremental"]["invalidated_count"],
        },
        "report": "report.json",
    }


def _run(arguments: argparse.Namespace) -> int:
    stop_event = threading.Event()
    previous_handlers: dict[int, Any] = {}

    def handle_signal(signum: int, _frame: Any) -> None:
        stop_event.set()
        print(
            f"received signal {signum}; finishing the current stage and committing an atomic snapshot",
            file=sys.stderr,
        )

    for signal_name in ("SIGINT", "SIGTERM"):
        candidate = getattr(signal, signal_name, None)
        if candidate is None:
            continue
        try:
            previous_handlers[candidate] = signal.signal(candidate, handle_signal)
        except (OSError, ValueError):
            pass
    try:
        outcome = run_pipeline(
            sample_dir=arguments.samples,
            config_path=arguments.config,
            pairs_path=arguments.pairs,
            anomalies_path=arguments.anomalies,
            output_dir=arguments.output,
            resume=arguments.resume,
            stop_event=stop_event,
        )
    except ConfigMismatchError as exc:
        output = Path(arguments.output)
        refusal = {
            "status": "refused",
            "code": exc.code,
            "message": "E_CONFIG_MISMATCH: refusing to resume with a different global version/config/injection hash",
            "detail": exc.detail,
        }
        try:
            write_json_atomic(output / "refusal.json", refusal)
        except OSError:
            pass
        print(json.dumps(refusal, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 3
    except (ConfigError, PipelineInputError, FileNotFoundError, ValueError) as exc:
        code = getattr(exc, "code", "E_INPUT")
        payload = {"status": "error", "code": code, "message": str(exc)}
        detail = getattr(exc, "detail", None)
        if detail is not None:
            payload["detail"] = detail
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass
    if outcome.status == "complete" and outcome.report is not None:
        if not arguments.quiet:
            print(json.dumps(_summary(outcome.report), ensure_ascii=False, sort_keys=True))
        return outcome.exit_code
    if not arguments.quiet:
        print(
            json.dumps(
                {
                    "status": outcome.status,
                    "exit_code": outcome.exit_code,
                    "message": outcome.message,
                    "snapshot": str(Path(arguments.output) / "state" / "state.json"),
                    "report_written": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return outcome.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "generate-demo":
        result = generate_demo(arguments.output, arguments.seed)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if arguments.command == "run":
        return _run(arguments)
    parser.error("unknown command")
    return 2

