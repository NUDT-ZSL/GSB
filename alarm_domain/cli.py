"""Command-line entry points for the demo and optional diagnostic view."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .demo import run_demo
from .errors import DomainError
from .logging_utils import configure_json_logging, log_business_error
from .tkinter_ui import UiUnavailable, launch_diagnostic_view
from .util import pretty_json


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m alarm_domain",
        description="Local alarm denoising and root-cause correlation component",
    )
    subcommands = parser.add_subparsers(dest="command")

    demo = subcommands.add_parser("demo", help="run the deterministic three-scenario demo")
    demo.add_argument("--samples-dir", default=None)
    demo.add_argument("--work-dir", default=None)
    demo.add_argument("--inject-time-regression", action="store_true")
    demo.add_argument("--inject-missing-snapshot-version", action="store_true")

    ui = subcommands.add_parser("ui", help="run the demo and open the Tkinter diagnostic view")
    ui.add_argument("--samples-dir", default=None)
    ui.add_argument("--work-dir", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    command = args.command or "demo"
    work_dir = Path(args.work_dir) if getattr(args, "work_dir", None) else None
    try:
        if command == "ui":
            result = run_demo(samples_dir=args.samples_dir, work_dir=work_dir)
            print(
                pretty_json(
                    {
                        "status": "opening-diagnostic-view",
                        "artifact": str((work_dir or Path("artifacts/demo")) / "diagnostic.json"),
                    }
                )
            )
            launch_diagnostic_view(result["diagnostic"])
            return 0

        if command == "demo":
            result = run_demo(
                samples_dir=args.samples_dir,
                work_dir=work_dir,
                inject_time_regression=args.inject_time_regression,
                inject_missing_snapshot_version=args.inject_missing_snapshot_version,
            )
            print(pretty_json(result))
            return 0
        parser.error(f"unknown command {command}")
        return 2
    except UiUnavailable as exc:
        print(pretty_json({"status": "error", "code": "E_UI", "message": str(exc)}))
        return 2
    except DomainError as exc:
        log_path = (work_dir or Path("artifacts/demo")) / "structured.jsonl"
        logger = configure_json_logging(log_path)
        log_business_error(logger, exc, event="cli_business_error")
        print(pretty_json({"status": "error", **exc.as_dict()}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
