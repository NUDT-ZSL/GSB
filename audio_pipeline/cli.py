"""Command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import PipelineError
from .pipeline import PipelineOptions, run_pipeline
from .seed import SEED, generate_fixtures


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _add_workspace(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", type=Path, default=Path("demo"),
                        help="workspace directory (default: ./demo)")
    parser.add_argument("--force", action="store_true",
                        help="regenerate files if fixtures already exist")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m audio_pipeline",
        description="Deterministic broadcast loudness delivery pipeline",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    seed_parser = sub.add_parser("seed", help="generate deterministic fixtures")
    _add_workspace(seed_parser)

    run_parser = sub.add_parser("run", help="run a measurement task")
    run_parser.add_argument("task_positional", nargs="?", type=Path)
    run_parser.add_argument("--task", type=Path, default=None)
    run_parser.add_argument("--workspace-root", "--workspace", type=Path, default=None)
    run_parser.add_argument("--output-dir", "--output", type=Path, default=None)
    run_parser.add_argument("--state-dir", "--state", type=Path, default=None)
    run_parser.add_argument("--resume", action="store_true",
                            help="reuse valid persisted chunks")
    run_parser.add_argument(
        "--simulate-interrupt-after-chunks",
        type=int,
        default=None,
        help="test hook: interrupt at a persisted chunk boundary",
    )

    demo_parser = sub.add_parser("demo", help="generate fixtures and run immediately")
    _add_workspace(demo_parser)
    demo_parser.add_argument("--simulate-interrupt-after-chunks", type=int,
                             default=None)

    sub.add_parser("self-check", help="validate imports and sqlite availability")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "seed":
            _print_json(generate_fixtures(args.workspace, force=args.force))
            return 0
        if args.command == "self-check":
            import sqlite3
            import numpy
            from .seed import SEED as seed_value
            _print_json({
                "ok": True,
                "python": sys.version.split()[0],
                "numpy": numpy.__version__,
                "sqlite": sqlite3.sqlite_version,
                "seed": seed_value,
            })
            return 0
        if args.command == "demo":
            generate_fixtures(args.workspace, force=args.force)
            task_path = args.workspace.resolve() / "task.json"
            outcome = run_pipeline(task_path, PipelineOptions(
                workspace_root=args.workspace.resolve(),
                simulate_interrupt_after_chunks=args.simulate_interrupt_after_chunks,
            ))
        else:
            task_path = args.task or args.task_positional
            if task_path is None:
                parser.error("run requires --task PATH or a positional task path")
            outcome = run_pipeline(task_path, PipelineOptions(
                workspace_root=args.workspace_root,
                output_dir=args.output_dir,
                state_dir=args.state_dir,
                resume=args.resume,
                simulate_interrupt_after_chunks=args.simulate_interrupt_after_chunks,
            ))
        summary = {
            "status": outcome.report.get("status"),
            "exit_code": outcome.exit_code,
            "report": str(outcome.report_path) if outcome.report_path else None,
            "summary": outcome.report.get("summary"),
            "first_error": outcome.report.get("first_error"),
            "interruption": outcome.report.get("interruption"),
        }
        _print_json(summary)
        return outcome.exit_code
    except PipelineError as exc:
        _print_json({"ok": False, "error": exc.as_dict()})
        if exc.code == "E_INTERRUPTED":
            signum = int(exc.details.get("signal", 2))
            return 128 + signum
        return 2 if exc.code in ("E_CONFIG", "E_INPUT") else 4
    except (OSError, FileExistsError, ValueError, json.JSONDecodeError) as exc:
        _print_json({"ok": False, "error": {"code": "E_IO", "message": str(exc)}})
        return 4
