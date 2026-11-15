from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .canonical import canonical_json
from .compare import compare_reports
from .config import ConfigError, load_config
from .parser import load_event_stream
from .report import build_report, report_to_bytes, verify_report_hash, write_report
from .samples import write_sample_bundle
from .scheduler import MIB, SchedulerBudget, run_scheduled
from .system_info import system_summary

DEFAULT_THREADS = 4
DEFAULT_MEMORY_BUDGET_MIB = 64
DEFAULT_PER_MATCH_MEMORY_MIB = 8


def _budget_from_args(args: argparse.Namespace) -> SchedulerBudget:
    return SchedulerBudget.from_mib(
        threads=args.threads,
        memory_budget_mib=args.memory_budget_mib,
        per_match_memory_mib=args.per_match_memory_mib,
    )


def _budget_echo(budget: SchedulerBudget) -> dict[str, int]:
    return {
        "thread_budget": budget.thread_budget,
        "memory_budget_bytes": budget.memory_budget_bytes,
        "per_match_memory_bytes": budget.per_match_memory_bytes,
    }


def _validate_files(
    *,
    events_path: str | Path,
    config_path: str | Path,
    budget: SchedulerBudget,
    output_path: str | Path,
) -> dict[str, Any]:
    config = load_config(config_path)
    parsed = load_event_stream(events_path)
    scheduling = run_scheduled(parsed.ordered_matches(), config, budget)
    report = build_report(
        config=config,
        parsed=parsed,
        scheduling=scheduling,
        scheduler_budget_echo=_budget_echo(budget),
    )
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_report(str(output), report)
    return report


def _print_json_line(value: dict[str, Any]) -> None:
    print(canonical_json(value))


def _command_generate(args: argparse.Namespace) -> int:
    config_path, events_path = write_sample_bundle(args.out_dir)
    _print_json_line(
        {
            "command": "generate",
            "config": str(config_path),
            "events": str(events_path),
        }
    )
    return 0


def _command_demo(args: argparse.Namespace) -> int:
    config_path, events_path = write_sample_bundle(args.out_dir)
    report_path = Path(args.out_dir) / "report.json"
    report = _validate_files(
        events_path=events_path,
        config_path=config_path,
        budget=_budget_from_args(args),
        output_path=report_path,
    )
    _print_json_line(
        {
            "command": "demo",
            "config": str(config_path),
            "events": str(events_path),
            "report": str(report_path),
            "content_hash": report["content_hash"],
            "summary": report["summary"],
            "scheduler": report["scheduler"],
        }
    )
    return 0


def _command_verify(args: argparse.Namespace) -> int:
    report = _validate_files(
        events_path=args.events,
        config_path=args.config,
        budget=_budget_from_args(args),
        output_path=args.out,
    )
    _print_json_line(
        {
            "command": "verify",
            "report": str(args.out),
            "content_hash": report["content_hash"],
            "summary": report["summary"],
            "scheduler": report["scheduler"],
        }
    )
    return 0


def _load_report(path: str | Path) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("report root must be an object")
    if not verify_report_hash(raw):
        raise ValueError(f"report content hash verification failed: {path}")
    return raw


def _command_compare(args: argparse.Namespace) -> int:
    left = _load_report(args.left)
    right = _load_report(args.right)
    comparison = compare_reports(left, right)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_bytes(report_to_bytes(comparison))
    _print_json_line(
        {
            "command": "compare",
            "report": str(args.out),
            "verdict": comparison["verdict"],
            "message": comparison["message"],
            "content_hash": comparison["content_hash"],
            "first_state_divergence": comparison["first_state_divergence"],
            "first_trace_divergence": comparison["first_trace_divergence"],
        }
    )
    return 0 if comparison["consistent"] else 3


def _command_info(_: argparse.Namespace) -> int:
    _print_json_line(
        {
            "command": "info",
            "system": system_summary(),
            "recommended_fixed_defaults": {
                "threads": DEFAULT_THREADS,
                "memory_budget_mib": DEFAULT_MEMORY_BUDGET_MIB,
                "per_match_memory_mib": DEFAULT_PER_MATCH_MEMORY_MIB,
                "memory_budget_bytes": DEFAULT_MEMORY_BUDGET_MIB * MIB,
                "per_match_memory_bytes": DEFAULT_PER_MATCH_MEMORY_MIB * MIB,
            },
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="card-replay",
        description="Deterministic replay validator for turn-based card battle JSONL streams.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_budget_options(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--threads",
            type=int,
            default=DEFAULT_THREADS,
            help="maximum worker threads (default: 4)",
        )
        command.add_argument(
            "--memory-budget-mib",
            type=int,
            default=DEFAULT_MEMORY_BUDGET_MIB,
            help="aggregate worker memory budget in MiB (default: 64)",
        )
        command.add_argument(
            "--per-match-memory-mib",
            type=int,
            default=DEFAULT_PER_MATCH_MEMORY_MIB,
            help="single-match memory reservation in MiB (default: 8)",
        )

    generate_parser = subparsers.add_parser(
        "generate", help="generate deterministic synthetic events and rules"
    )
    generate_parser.add_argument("--out-dir", default="artifacts/demo")
    generate_parser.set_defaults(handler=_command_generate)

    demo_parser = subparsers.add_parser(
        "demo",
        help="generate samples and validate them with fixed budgets",
    )
    demo_parser.add_argument("--out-dir", default="artifacts/demo")
    add_budget_options(demo_parser)
    demo_parser.set_defaults(handler=_command_demo)

    verify_parser = subparsers.add_parser("verify", help="replay an existing event stream")
    verify_parser.add_argument("--events", required=True)
    verify_parser.add_argument("--config", required=True)
    verify_parser.add_argument("--out", required=True)
    add_budget_options(verify_parser)
    verify_parser.set_defaults(handler=_command_verify)

    compare_parser = subparsers.add_parser(
        "compare", help="locate the first replay divergence between two reports"
    )
    compare_parser.add_argument("left")
    compare_parser.add_argument("right")
    compare_parser.add_argument("--out", required=True)
    compare_parser.set_defaults(handler=_command_compare)

    info_parser = subparsers.add_parser(
        "info", help="show detected CPU/memory and fixed budget defaults"
    )
    info_parser.set_defaults(handler=_command_info)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ConfigError, ValueError, OSError) as exc:
        print(
            canonical_json(
                {
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "violation": getattr(exc, "violation", None).to_dict()
                        if getattr(exc, "violation", None) is not None
                        else None,
                    }
                }
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
