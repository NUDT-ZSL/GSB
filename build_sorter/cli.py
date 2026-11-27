"""Command-line interface for the build-task external sorter."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import SortConfig
from .engine import (
    cleanup_after_failure,
    run_all,
    run_merge_phase,
    run_spill_phase,
)
from .errors import SorterError
from .manifest import MANIFEST_FILENAME, load_manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build-sorter",
        description=(
            "External-sort SQLite build_task rows by "
            "(trigger_ts ASC, input sequence ASC)."
        ),
    )
    parser.add_argument(
        "--phase",
        choices=("run", "spill", "merge"),
        default="run",
        help="run spill then merge, only spill, or merge an existing manifest",
    )
    parser.add_argument("--db", type=Path, help="source SQLite database")
    parser.add_argument(
        "--mem-limit-bytes",
        type=int,
        help="hard limit for serialized in-memory records",
    )
    parser.add_argument(
        "--max-fan-in",
        type=int,
        help="maximum number of runs in one merge step",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        required=True,
        help="directory for manifest, segment files, and the default report",
    )
    parser.add_argument(
        "--source-table",
        default=None,
        help="source table name (default: build_task)",
    )
    parser.add_argument(
        "--result-table",
        default=None,
        help="result table name (default: build_task_sorted)",
    )
    parser.add_argument(
        "--output-db",
        type=Path,
        default=None,
        help="database for the sorted result table (default: source database)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="accounting JSON path (default: WORK_DIR/sort_report.json)",
    )
    parser.add_argument(
        "--checkpoint-records",
        type=int,
        default=1000,
        help="commit and checkpoint the final merge every N rows",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume from a manifest in WORK_DIR when available",
    )
    parser.add_argument(
        "--skip-segment-verify",
        action="store_true",
        help="skip full SHA-256 and record rescanning during merge startup",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="do not print the report or phase summary to stdout",
    )
    return parser


def _manifest_defaults(work_dir: Path) -> dict[str, Any]:
    path = work_dir / MANIFEST_FILENAME
    if not path.is_file():
        return {}
    return load_manifest(work_dir)


def _build_config(args: argparse.Namespace) -> SortConfig:
    manifest_data: dict[str, Any] = {}
    if args.phase == "merge" or (args.phase == "run" and args.resume):
        manifest_data = _manifest_defaults(args.work_dir)

    db_path = args.db
    if db_path is None and manifest_data:
        db_path = Path(manifest_data["db_path"])
    if db_path is None:
        raise SorterError(
            "MISSING_DATABASE",
            "--db is required when no manifest is available",
        )

    mem_limit = (
        args.mem_limit_bytes
        if args.mem_limit_bytes is not None
        else manifest_data.get("mem_limit_bytes")
    )
    if mem_limit is None:
        raise SorterError(
            "MISSING_MEMORY_LIMIT",
            "--mem-limit-bytes is required for a fresh spill",
        )
    max_fan_in = (
        args.max_fan_in
        if args.max_fan_in is not None
        else manifest_data.get("max_fan_in", 8)
    )
    source_table = (
        args.source_table
        if args.source_table is not None
        else manifest_data.get("source_table", "build_task")
    )
    result_table = (
        args.result_table
        if args.result_table is not None
        else manifest_data.get("result_table", "build_task_sorted")
    )
    output_db = args.output_db
    if output_db is None and manifest_data:
        output_db = Path(manifest_data["output_db_path"])
    report = args.report
    if report is None and manifest_data.get("report_path"):
        report = Path(manifest_data["report_path"])

    return SortConfig(
        db_path=db_path,
        work_dir=args.work_dir,
        mem_limit_bytes=int(mem_limit),
        max_fan_in=int(max_fan_in),
        source_table=str(source_table),
        result_table=str(result_table),
        output_db_path=output_db,
        report_path=report,
        checkpoint_records=args.checkpoint_records,
        resume=args.resume,
        verify_segments=not args.skip_segment_verify,
    )


def _print_json(value: Any) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    config: SortConfig | None = None
    try:
        config = _build_config(args)
        if args.phase == "spill":
            manifest = run_spill_phase(config)
            if not args.quiet:
                _print_json(
                    {
                        "phase": "spill",
                        "manifest": str(config.work_dir / MANIFEST_FILENAME),
                        "spill_segment_count": manifest["spill_segment_count"],
                        "spill_segment_record_counts": manifest[
                            "spill_segment_record_counts"
                        ],
                        "spill_temp_bytes": manifest["spill_temp_bytes"],
                        "peak_memory_bytes": manifest["spill_peak_memory_bytes"],
                    }
                )
            return 0
        if args.phase == "merge":
            report = run_merge_phase(config)
        else:
            report = run_all(config)
        if not args.quiet:
            _print_json(report)
        return 0
    except SorterError as exc:
        cleanup_after_failure(config)
        print(
            json.dumps(
                {"error": exc.as_dict()},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return exc.exit_code
    except Exception as exc:
        cleanup_after_failure(config)
        print(
            json.dumps(
                {
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": str(exc),
                        "details": {"type": type(exc).__name__},
                    }
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 5
