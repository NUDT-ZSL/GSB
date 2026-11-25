"""Command line interface for batch runs and interactive doc queries."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .canonical import sha256_bytes
from .config import load_config
from .engine import DedupWorkbench
from .errors import WorkbenchError
from .query import QueryService


def json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__bytes_sha256__": sha256_bytes(value), "length": len(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=json_default))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dedup-workbench", description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    run_parser = subcommands.add_parser("run", help="run the full batch graph")
    run_parser.add_argument("--input", type=Path, required=True)
    run_parser.add_argument("--config", type=Path, required=True)
    run_parser.add_argument("--workdir", type=Path, required=True)
    run_parser.add_argument("--report", type=Path)
    run_parser.add_argument("--kill-after-batch", type=int, help=argparse.SUPPRESS)

    query_parser = subcommands.add_parser("query", help="query one doc_id")
    query_parser.add_argument("--workdir", type=Path, required=True)
    query_parser.add_argument("--doc-id", required=True)
    query_parser.add_argument("--config", type=Path)
    query_parser.add_argument("--budget-ms", type=float)
    query_parser.add_argument("--iterations", type=int)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            workbench = DedupWorkbench(args.workdir, args.config)
            report = workbench.run(args.input, kill_after_batch=args.kill_after_batch)
            report_path = args.report or args.workdir / "run_report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=json_default) + "\n",
                encoding="utf-8",
            )
            print_json(
                {
                    "ok": True,
                    "report_path": str(report_path),
                    "stats": report["stats"],
                    "incremental": {
                        key: report["incremental"][key]
                        for key in ("recomputed_docs", "reused_docs", "recomputed_nodes", "reused_nodes", "disjoint", "covers_all_docs")
                    },
                    "self_check": {"ok": report["self_check"]["ok"], "check_count": report["self_check"]["check_count"]},
                }
            )
            return 0

        if args.command == "query":
            config = load_config(args.config) if args.config else None
            service = QueryService(args.workdir / "exports" / "doc_records.jsonl", config)
            result = service.query(args.doc_id, budget_ms=args.budget_ms, iterations=args.iterations)
            print_json(
                {
                    "ok": True,
                    "doc_id": result["doc_id"],
                    "record": result["record"],
                    "record_sha256": result["record_sha256"],
                    "byte_identical_to_batch_line": result["byte_identical_to_batch_line"],
                    "timing": result["timing"],
                }
            )
            return 0

        raise AssertionError(args.command)
    except WorkbenchError as exc:
        print_json(exc.to_dict())
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
