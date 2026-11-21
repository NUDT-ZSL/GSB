"""Command-line entry points."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .compare import compare_paths
from .errors import QUError
from .experiments import run_experiments
from .pipeline import QueryEngine
from .scale import ScalePolicy, choose_path, observe_scale
from .budget import BudgetPolicy
from .util import pretty_json, read_json, read_jsonl


def _print(obj: Any) -> None:
    sys.stdout.write(pretty_json(obj))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m query_understanding",
        description="Deterministic query understanding and scale-aware offline evaluation.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run the full reproducible experiment")
    run_parser.add_argument("--data", default="data")
    run_parser.add_argument("--reports", default="reports")

    process_parser = subparsers.add_parser("process", help="process one JSON query")
    process_parser.add_argument("--data", default="data")
    process_parser.add_argument("--query", required=True)
    process_parser.add_argument(
        "--path", choices=["exact", "pruned_approx"], default="exact"
    )

    compare_parser = subparsers.add_parser(
        "compare", help="compare exact and approximate paths on a JSONL log"
    )
    compare_parser.add_argument("--data", default="data")
    compare_parser.add_argument("--queries", default="data/query_log_small.jsonl")

    scale_parser = subparsers.add_parser(
        "scale", help="show a deterministic scale decision for an explicit observation"
    )
    scale_parser.add_argument("--data", default="data")
    scale_parser.add_argument("--queries", default="data/query_log_large.jsonl")
    scale_parser.add_argument("--resident-memory-mb", type=float, default=48.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            report = run_experiments(args.data, args.reports)
            _print(
                {
                    "ok": report["verification"]["all_passed"],
                    "report_core_checksum": report["report_core_checksum"],
                    "report_json": str(Path(args.reports) / "experiment_report.json"),
                    "small_metrics": report["experiments"]["small"]["metrics"][
                        "aggregate"
                    ],
                    "large_metrics": report["experiments"]["large"]["metrics"][
                        "aggregate"
                    ],
                    "path_comparable": report["path_comparison"]["comparable"],
                }
            )
            return 0 if report["verification"]["all_passed"] else 1
        config = read_json(Path(args.data) / "config.json")
        engine = QueryEngine.from_files(args.data, config)
        if args.command == "process":
            query = {
                "query_id": "cli-0001",
                "query_text": args.query,
                "session_id": "cli-session",
                "timestamp_ms": 0,
                "locale": "zh-CN",
                "raw_clicks": [],
            }
            result = engine.process(query, path=args.path)
            _print(result)
            return 0
        queries = read_jsonl(args.queries)
        if args.command == "compare":
            report = compare_paths(engine, queries)
            _print(
                {
                    "ok": report["ok"],
                    "error_code": report["error_code"],
                    "intent_agreement_rate": report["intent_agreement_rate"],
                    "slot_offset_difference_count": report[
                        "slot_offset_difference_count"
                    ],
                    "confidence_bias": report["confidence_bias_spectrum"],
                    "metrics": {
                        "exact": report["metrics"]["exact"]["aggregate"],
                        "approx": report["metrics"]["pruned_approx"]["aggregate"],
                    },
                }
            )
            return 0 if report["ok"] else 1
        if args.command == "scale":
            total_tokens = 0
            from .tokenize import tokenize_text

            for query in queries:
                normalized = engine.mappings.normalize(query["query_text"])
                total_tokens += len(tokenize_text(normalized, engine.dictionary))
            observation = observe_scale(
                query_count=len(queries),
                dictionary_entry_count=len(engine.dictionary.entries),
                total_tokens=total_tokens,
                memory_limit_mb=float(config["budget_policy"]["memory_limit_mb"]),
                resident_memory_mb=args.resident_memory_mb,
                memory_reserve_mb=float(config["evaluation"]["memory_reserve_mb"]),
                resident_memory_source="cli_explicit",
            )
            decision = choose_path(
                observation,
                ScalePolicy.from_dict(config["scale_policy"]),
                BudgetPolicy.from_dict(config["budget_policy"]),
            )
            _print(decision)
            return 0 if decision["ok"] else 1
    except QUError as error:
        sys.stderr.write(pretty_json(error.to_dict()))
        return 2
    return 1

