"""Command-line example for local deterministic variant arbitration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .cache import ContentAddressedCache
from .engine import EngineLimits
from .errors import DomainError, as_domain_error
from .hashing import atomic_write_bytes
from .models import SnapshotStore
from .sample import build_sample_cases, load_cases_jsonl, write_cases_jsonl
from .variants import build_builtin_mapping_registry
from .workflow import run_snapshot_workflow


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run local deterministic RoPE variant arbitration and emit a JSON report."
        )
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("data/sample_cases.jsonl"),
        help="JSONL task list (default: data/sample_cases.jsonl)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".cache/rope-arbiter"),
        help="content-addressed cache directory",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=Path("reports"),
        help="JSON report output directory",
    )
    parser.add_argument(
        "--report-name",
        default="variant_arbitration_report.json",
        help="report file name",
    )
    parser.add_argument(
        "--max-cache-entries",
        type=int,
        default=4096,
        help="maximum unique content-addressed cache entries",
    )
    parser.add_argument(
        "--max-parallel-nodes",
        type=int,
        default=4,
        help="configured parallel recompute ceiling",
    )
    parser.add_argument(
        "--parallelism",
        type=int,
        default=8,
        help="requested case/task parallelism; degradation is reported if above the ceiling",
    )
    parser.add_argument(
        "--max-attention-memory-mb",
        type=int,
        default=128,
        help="per-call attention peak-memory limit in MiB",
    )
    parser.add_argument(
        "--mapping-tolerance",
        type=float,
        default=1e-9,
        help="maximum circular angle-mapping residual",
    )
    parser.add_argument(
        "--block-tolerance",
        type=float,
        default=1e-9,
        help="maximum block-vs-full element deviation",
    )
    parser.add_argument(
        "--score-tolerance",
        type=float,
        default=1e-6,
        help="cross-variant score and attention-output equivalence tolerance",
    )
    parser.add_argument(
        "--generate-sample",
        action="store_true",
        help="write the built-in deterministic sample to --cases and exit",
    )
    return parser


def _write_report(path: Path, report: dict[str, Any]) -> None:
    blob = (
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, blob)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.generate_sample:
            write_cases_jsonl(args.cases, build_sample_cases())
            print(json.dumps({"status": "ok", "cases": str(args.cases)}))
            return 0

        cases = load_cases_jsonl(args.cases)
        args.cache_dir.mkdir(parents=True, exist_ok=True)
        args.reports_dir.mkdir(parents=True, exist_ok=True)
        cache = ContentAddressedCache(
            args.cache_dir,
            max_entries=args.max_cache_entries,
        )
        limits = EngineLimits(
            max_attention_memory_bytes=args.max_attention_memory_mb * 1024 * 1024,
            max_parallel_recompute_nodes=args.max_parallel_nodes,
            mapping_tolerance=args.mapping_tolerance,
            block_tolerance=args.block_tolerance,
            score_tolerance=args.score_tolerance,
        )
        report = run_snapshot_workflow(
            cases=cases,
            cache=cache,
            stage_store=SnapshotStore(args.cache_dir / "snapshot_stage"),
            limits=limits,
            parallelism=args.parallelism,
            mapping_registry=build_builtin_mapping_registry(),
        )
        report_path = args.reports_dir / args.report_name
        _write_report(report_path, report)
        summary = {
            "status": "ok" if report["self_check"]["ok"] else "self_check_failed",
            "report": str(report_path),
            "snapshots": report["self_check"]["transition_count"],
            "case_count": len(cases),
            "cache_entries": report["final_snapshot"]["cache_entry_count"],
            "business_codes": report["business_codes"],
            "degradation_count": len(report["degradations"]),
        }
        print(json.dumps(summary, sort_keys=True))
        return 0 if report["self_check"]["ok"] else 1
    except DomainError as exc:
        print(json.dumps(exc.to_dict(), sort_keys=True), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps(as_domain_error(exc).to_dict(), sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
