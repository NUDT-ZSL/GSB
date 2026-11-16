"""Command-line interface for reproducible local SpMM validation runs."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Sequence

from .errors import SpMMError
from .injection import run_injections
from .reporting import finalize_status
from .runner import prepare_case, run_case
from .utils import atomic_write_json


DEFAULT_SAMPLE = "config/sample_scsr.json"
DEFAULT_STRATEGIES = "config/strategies.json"
DEFAULT_INJECTIONS = "config/injections.json"
DEFAULT_REPORT = "reports/scsr_validation_report.json"
DEFAULT_PROGRESS = "reports/scsr_progress.json"


def _load_json(path: str) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sparse_spmm",
        description="Deterministic CSR SpMM precision attribution and sensitivity report",
    )
    subparsers = parser.add_subparsers(dest="command")
    run_parser = subparsers.add_parser("run", help="run the local deterministic sample")
    run_parser.add_argument("--sample", default=DEFAULT_SAMPLE)
    run_parser.add_argument("--strategies", default=DEFAULT_STRATEGIES)
    run_parser.add_argument("--injections", default=DEFAULT_INJECTIONS)
    run_parser.add_argument("--report", default=DEFAULT_REPORT)
    run_parser.add_argument("--progress", default=DEFAULT_PROGRESS)
    run_parser.add_argument("--max-workers", type=int, default=None)
    run_parser.add_argument("--fresh", action="store_true", help="discard an existing checkpoint")
    run_parser.add_argument(
        "--stop-after-blocks",
        type=int,
        default=None,
        help="gracefully stop after N completed row blocks (deterministic resume test)",
    )
    run_parser.add_argument(
        "--skip-injections", action="store_true", help="do not execute the isolated injection samples"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command is None:
        args = _build_parser().parse_args(["run"])

    stop_event = threading.Event()
    signal_state: dict[str, str | None] = {"name": None}
    previous_handlers: dict[int, Any] = {}

    def handle_signal(signum: int, _frame: Any) -> None:
        signal_state["name"] = signal.Signals(signum).name
        stop_event.set()

    for candidate in ("SIGINT", "SIGTERM"):
        if hasattr(signal, candidate):
            signal_number = getattr(signal, candidate)
            try:
                previous_handlers[signal_number] = signal.signal(signal_number, handle_signal)
            except (OSError, ValueError):
                pass

    try:
        sample_config = _load_json(args.sample)
        strategy_config = _load_json(args.strategies)
        injection_config = None if args.skip_injections else _load_json(args.injections)
        case = prepare_case(sample_config, strategy_config)
        max_workers = (
            int(args.max_workers)
            if args.max_workers is not None
            else int(sample_config.get("execution", {}).get("max_workers", 4))
        )
        outcome = run_case(
            case,
            report_path=args.report,
            progress_path=args.progress,
            max_workers=max_workers,
            fresh=bool(args.fresh),
            stop_event=stop_event,
            interrupt_after_blocks=args.stop_after_blocks,
            external_stop_reason=lambda: signal_state["name"],
        )
        if outcome.status == "INTERRUPTED":
            print(
                f"INTERRUPTED: completed {outcome.progress['completed_row_count']} rows; "
                f"checkpoint={outcome.progress_path}; exit_code=0; resume with the same command"
            )
            return 0

        report = outcome.report
        assert report is not None
        if injection_config is not None:
            injection_result = run_injections(case, injection_config)
            report["injections"] = {
                key: value for key, value in injection_result.items() if key != "failures"
            }
            finalize_status(report, injection_result["failures"])
            atomic_write_json(args.report, report)
        else:
            report["injections"] = {
                "attempted_count": 0,
                "hit_count": 0,
                "attempted_ids": [],
                "hit_ids": [],
                "all_expected_codes_hit": True,
                "records": [],
            }
            finalize_status(report, [])
            atomic_write_json(args.report, report)
        print(
            f"PASS: report={outcome.report_path}; elements={report['attribution']['element_count']}; "
            f"violations={report['statistics']['tolerance_violation_count']}; "
            f"injection_hits={report['injections']['hit_count']}/{report['injections']['attempted_count']}"
        )
        return 0
    except SpMMError as exc:
        failure = exc.to_failure()
        try:
            atomic_write_json(
                args.report,
                {
                    "schema_version": 1,
                    "status": "FAIL",
                    "business_code": exc.business_code,
                    "failures": [failure],
                },
            )
        except Exception:
            pass
        print(f"FAIL: {exc.business_code}: {exc.message}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"FAIL: E_CONFIG_INVALID: {exc}", file=sys.stderr)
        return 2
    finally:
        for signal_number, handler in previous_handlers.items():
            try:
                signal.signal(signal_number, handler)
            except (OSError, ValueError):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
