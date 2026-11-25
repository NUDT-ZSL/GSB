"""Command-line interface for local runs, queries, migrations, and diagnostics."""

from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path

import numpy as np

from .budget import Budgets
from .configs import migrate_v1_to_v2, migrate_v2_to_v1, read_v1_document, write_v1_document, write_v2_snapshot
from .demo import run_demo
from .diagnostics import run_selfcheck
from .engine import Engine
from .errors import AppError
from .query import query_case
from .util import atomic_write_json


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def _run(args: argparse.Namespace) -> int:
    result = Engine(
        args.input_dir,
        args.out_dir,
        mode=args.mode,
        global_seed=args.seed,
        budgets=Budgets(
            total_feature_buffer_size=args.total_feature_buffer_size,
            single_pass_workspace_size=args.single_pass_workspace_size,
            model_workspace_size=args.model_workspace_size,
        ),
        checkpoint_batch_tasks=args.checkpoint_batch,
        logical_window_ms=args.logical_window_ms,
        simulate_crash_after_tasks=args.simulate_crash_after_tasks,
        simulate_crash_after_stages=args.simulate_crash_after_stages,
        simulate_crash_after_cache_commit=args.simulate_crash_after_cache_commit,
    ).run()
    _print(result.to_dict())
    return 0


def _query(args: argparse.Namespace) -> int:
    result = query_case(
        args.out_dir,
        args.case_id,
        mode=args.mode,
        export_npz=args.export_npz,
        input_dir=args.input_dir,
    )
    _print(result)
    return 0


def _migrate(args: argparse.Namespace) -> int:
    output_dir = Path(args.out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.direction == "v1-to-v2":
        document = read_v1_document(args.config_path)
        result = migrate_v1_to_v2(
            document,
            base_epoch_ms=args.base_epoch_ms,
            target_generation=args.target_generation,
        )
        write_v2_snapshot(
            output_dir / "v2",
            generations=[
                {
                    "config_generation": result["config_generation"],
                    "effective_at_ms": result["effective_at_ms"],
                    "shards": result["shards"],
                }
            ],
            tombstones=result["tombstones"],
        )
    else:
        result = migrate_v2_to_v1(
            args.config_path,
            base_epoch_ms=args.base_epoch_ms,
        )
        write_v1_document(output_dir / "config_v1.json", result["v1"])
    atomic_write_json(output_dir / "migration_report.json", result)
    _print(
        {
            "expected_result": result.get("migration", result.get("expected_result")),
            "input": args.config_path,
            "out_dir": str(output_dir),
            "field_sources_count": len(result["field_sources"]),
            "filled_defaults": result["filled_defaults"],
            "dropped_fields": result["dropped_fields"],
            "report": str(output_dir / "migration_report.json"),
        }
    )
    return 0


def _selfcheck(args: argparse.Namespace) -> int:
    report = run_selfcheck(args.input_dir, args.out_dir, global_seed=args.seed)
    _print(
        {
            "overall_pass": report["overall_pass"],
            "summary": report["summary"],
            "report_checksum": report["report_checksum"],
            "report_files": report.get("report_files"),
        }
    )
    return 0 if report["overall_pass"] else 1


def _demo(args: argparse.Namespace) -> int:
    report = run_demo(
        args.input_dir,
        args.out_dir,
        recovery_dir=args.recovery_dir,
        global_seed=args.seed,
        clean=args.clean,
    )
    _print(
        {
            "all_acceptance_checks_pass": report["all_acceptance_checks_pass"],
            "normal": report["normal_run"]["status"],
            "forced": report["forced_run"]["status"],
            "resumed": report["resumed_run"]["status"],
            "recovery_comparison": {
                key: report["recovery_comparison"][key]
                for key in (
                    "all_equal",
                    "product_signatures_equal",
                    "cache_index_equal",
                    "completed_case_ids_equal",
                )
            },
            "selfcheck": report["selfcheck"],
            "report": str(Path(args.out_dir) / "reports" / "demo_report.json"),
        }
    )
    return 0 if report["all_acceptance_checks_pass"] else 1


def _inspect_env() -> int:
    _print(
        {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "numpy": np.__version__,
            "standard_libraries": ["sqlite3", "json", "threading", "multiprocessing"],
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m tii_app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("inspect-env")

    run = subparsers.add_parser("run", help="execute train/infer integrated batches")
    run.add_argument("--input-dir", default="input")
    run.add_argument("--out-dir", default="out")
    run.add_argument("--mode", choices=["train", "infer", "both"], default="both")
    run.add_argument("--seed", type=int, default=42)
    run.add_argument("--checkpoint-batch", type=int, default=2)
    run.add_argument("--logical-window-ms", type=int, default=1000)
    run.add_argument("--total-feature-buffer-size", type=int, default=64 * 1024 * 1024)
    run.add_argument("--single-pass-workspace-size", type=int, default=16 * 1024 * 1024)
    run.add_argument("--model-workspace-size", type=int, default=32 * 1024 * 1024)
    run.add_argument("--simulate-crash-after-tasks", type=int)
    run.add_argument("--simulate-crash-after-stages", type=int)
    run.add_argument("--simulate-crash-after-cache-commit", action="store_true")
    run.set_defaults(handler=_run)

    query = subparsers.add_parser("query", help="retrieve one committed case")
    query.add_argument("--input-dir", default="input")
    query.add_argument("--out-dir", default="out")
    query.add_argument("--case-id", required=True)
    query.add_argument("--mode", choices=["train", "infer", "both"], default="both")
    query.add_argument("--export-npz")
    query.set_defaults(handler=_query)

    migrate = subparsers.add_parser("migrate", help="bidirectional config migration")
    migrate.add_argument("--direction", choices=["v1-to-v2", "v2-to-v1"], required=True)
    migrate.add_argument("--config-path", required=True)
    migrate.add_argument("--out-dir", required=True)
    migrate.add_argument("--base-epoch-ms", type=int, default=0)
    migrate.add_argument("--target-generation", type=int)
    migrate.set_defaults(handler=_migrate)

    selfcheck = subparsers.add_parser("selfcheck", help="run deterministic cross-module checks")
    selfcheck.add_argument("--input-dir", default="input")
    selfcheck.add_argument("--out-dir", default="out")
    selfcheck.add_argument("--seed", type=int, default=42)
    selfcheck.set_defaults(handler=_selfcheck)

    demo = subparsers.add_parser("demo", help="normal + forced-stop + recovery acceptance run")
    demo.add_argument("--input-dir", default="input")
    demo.add_argument("--out-dir", default="out")
    demo.add_argument("--recovery-dir", default="out_recovery")
    demo.add_argument("--seed", type=int, default=42)
    demo.add_argument("--no-clean", action="store_false", dest="clean")
    demo.set_defaults(handler=_demo)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "inspect-env":
        return _inspect_env()
    try:
        return int(args.handler(args))
    except AppError as exc:
        _print(exc.to_dict())
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
