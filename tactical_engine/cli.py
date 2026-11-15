from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Sequence

from .errors import EngineError
from .pipeline import run_scenario_files, run_scenario_parts
from .sample_data import generate_sample


def _positive_or_zero_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tactical-sim",
        description="Deterministic tactical pathfinding, replay, and diagnostics engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="validate, plan, replay, and write artifacts")
    run_parser.add_argument(
        "--scenario",
        default=None,
        help="combined scenario JSON (default: samples/sample_scenario.json)",
    )
    run_parser.add_argument("--terrain", default=None, help="split terrain JSON")
    run_parser.add_argument("--units", default=None, help="split unit-list JSON")
    run_parser.add_argument("--replay", default=None, help="split replay-script JSON")
    run_parser.add_argument(
        "--options",
        default=None,
        help="split scene parameters JSON",
    )
    run_parser.add_argument("--output", default="output", help="output directory")
    run_parser.add_argument("--state", default="state", help="snapshot state directory")
    run_parser.add_argument(
        "--resume",
        default=None,
        help="snapshot JSON to continue from; an invalid version restarts from the beginning",
    )
    run_parser.add_argument(
        "--diagnostic-threshold",
        type=_positive_or_zero_float,
        default=1e-12,
        help="absolute path-cost error threshold used by diagnostics",
    )

    sample_parser = subparsers.add_parser(
        "generate-sample",
        help="write deterministic local sample files",
    )
    sample_parser.add_argument("--destination", default="samples")

    gui_parser = subparsers.add_parser("gui", help="launch the PySide6 desktop application")
    gui_parser.add_argument("--scenario", default="samples/sample_scenario.json")
    gui_parser.add_argument("--run-summary", default="output/summary.json")
    return parser


def _print_error(error: EngineError) -> None:
    print(json.dumps(error.to_dict(), ensure_ascii=False, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate-sample":
            paths = generate_sample(args.destination)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "files": {key: str(value) for key, value in paths.items()},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "gui":
            from .gui import main as gui_main

            return gui_main(
                scenario_path=args.scenario,
                run_summary_path=args.run_summary,
            )

        split_values = [args.terrain, args.units, args.replay, args.options]
        if any(value is not None for value in split_values):
            if not all(value is not None for value in split_values):
                parser.error(
                    "--terrain, --units, --replay, and --options must be supplied together"
                )
            summary = run_scenario_parts(
                args.terrain,
                args.units,
                args.replay,
                args.options,
                output_directory=args.output,
                state_directory=args.state,
                resume_snapshot_path=args.resume,
                diagnostic_threshold=args.diagnostic_threshold,
            )
        else:
            scenario_argument = args.scenario or "samples/sample_scenario.json"
            scenario_path = Path(scenario_argument)
            if (
                not scenario_path.exists()
                and scenario_argument == "samples/sample_scenario.json"
            ):
                generate_sample(scenario_path.parent)
            summary = run_scenario_files(
                scenario_path,
                output_directory=args.output,
                state_directory=args.state,
                resume_snapshot_path=args.resume,
                diagnostic_threshold=args.diagnostic_threshold,
            )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except EngineError as exc:
        _print_error(exc)
        return 2
    except Exception as exc:
        _print_error(
            EngineError(
                "E_INTERNAL",
                "unexpected engine failure",
                {"exception": type(exc).__name__, "reason": str(exc)},
            )
        )
        return 3
