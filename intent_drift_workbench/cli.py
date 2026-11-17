"""Command-line entry points for serial and parallel diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .canonical import atomic_write_json
from .comparison import compare_serial_parallel
from .config import ConfigError
from .engine import WorkbenchEngine
from .reporting import build_report, write_detail_artifacts, write_report


def _sample_paths(samples: Path) -> dict[str, Path]:
    return {
        "config": samples / "drift_config.json",
        "events": samples / "sessions.jsonl",
        "plan": samples / "update_plan.json",
        "injections": samples / "injections.json",
    }


def _engine(
    paths: dict[str, Path],
    *,
    snapshot_root: Path,
    execution: str,
    resume: bool,
) -> WorkbenchEngine:
    return WorkbenchEngine(
        config_path=paths["config"],
        events_path=paths["events"],
        plan_path=paths["plan"],
        injections_path=paths["injections"],
        snapshot_root=snapshot_root,
        execution=execution,
        resume=resume,
    )


def _print_summary(report_path: Path, result: dict[str, Any]) -> None:
    summary = {
        "status": "complete",
        "report": str(report_path),
        "execution": result.get("execution"),
        "snapshot": result.get("snapshot"),
        "counts": result.get("counts"),
        "equivalence": {
            "equal": (result.get("equivalence") or {}).get("equal"),
            "mismatch_count": (result.get("equivalence") or {}).get("mismatch_count"),
        },
        "boundary_alerts": len(result.get("boundary_alerts", [])),
        "injection_hits": {
            item["id"]: item["hit"] for item in result.get("injection_hits", [])
        },
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


def command_run(args: argparse.Namespace) -> int:
    samples = Path(args.samples).resolve()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    paths = _sample_paths(samples)
    for label, path in paths.items():
        if not path.is_file():
            print(json.dumps({"status": "error", "code": "E_INPUT_MISSING", "path": str(path), "input": label}))
            return 2

    if args.compare_concurrency:
        serial_engine = _engine(
            paths,
            snapshot_root=output / "serial" / "snapshots",
            execution="serial",
            resume=not args.no_resume,
        )
        parallel_engine = _engine(
            paths,
            snapshot_root=output / "parallel" / "snapshots",
            execution="parallel",
            resume=not args.no_resume,
        )
        serial_result = serial_engine.run(max_batches=args.max_batches)
        parallel_result = parallel_engine.run(max_batches=args.max_batches)
        concurrency = compare_serial_parallel(serial_result, parallel_result)
        concurrency["performed"] = True
        concurrency["serial_counts"] = serial_result.get("counts")
        concurrency["parallel_counts"] = parallel_result.get("counts")
        main_engine = parallel_engine
        main_result = parallel_result
        serial_result_for_report = serial_result
        artifact_dir = output / "parallel"
    else:
        engine = _engine(
            paths,
            snapshot_root=output / "snapshots",
            execution=args.execution,
            resume=not args.no_resume,
        )
        engine.prepare()
        result = engine.run(
            max_batches=args.max_batches,
            crash_after_batch=args.crash_after_batch,
        )
        if result.get("crash_requested"):
            snapshot = result.get("snapshot") or {}
            recovery = (
                engine.snapshot_manager.recovery_info(snapshot)
                if engine.snapshot_manager is not None and snapshot
                else None
            )
            print(json.dumps({
                "status": "terminated_after_snapshot",
                "exit_code": 137,
                "recovery_point": recovery,
                "note": "No exit report is promised for this forced-termination test path.",
            }, ensure_ascii=False, sort_keys=True), flush=True)
            os._exit(137)
        main_engine = engine
        main_result = result
        serial_result_for_report = None
        concurrency = None
        artifact_dir = output

    artifacts = write_detail_artifacts(artifact_dir, main_engine, main_result)
    report = build_report(
        engine=main_engine,
        result=main_result,
        serial_result=serial_result_for_report,
        concurrency_comparison=concurrency,
        samples_dir=samples,
        artifact_paths=artifacts,
    )
    report_path = write_report(output, report)
    atomic_write_json(output / "run_summary.json", {
        "report": str(report_path),
        "artifacts": artifacts,
        "concurrency_equal": None if concurrency is None else concurrency.get("equal"),
        "equivalence_equal": (main_result.get("equivalence") or {}).get("equal"),
    })
    _print_summary(report_path, main_result)
    if (main_result.get("equivalence") or {}).get("equal") is False:
        return 4
    if concurrency is not None and not concurrency.get("equal", False):
        return 5
    return 0


def command_show_recovery(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    manager_path = output / "snapshots"
    from .snapshot import SnapshotManager

    manager = SnapshotManager(manager_path)
    latest = manager.load_latest()
    if latest is None:
        print(json.dumps({"status": "no_complete_snapshot"}))
        return 1
    manifest, _ = latest
    print(json.dumps(manager.recovery_info(manifest), ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="intent-drift-workbench",
        description="Deterministic local intent-drift diagnostics workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run incremental and full-rebuild verification")
    run.add_argument("--samples", required=True, help="sample directory")
    run.add_argument("--output", required=True, help="workspace output directory")
    run.add_argument(
        "--execution",
        choices=("serial", "parallel"),
        default="parallel",
        help="single-thread or configured thread-pool pipeline",
    )
    run.add_argument(
        "--compare-concurrency",
        action="store_true",
        help="run serial and parallel pipelines in separate snapshot roots and compare summaries",
    )
    run.add_argument("--max-batches", type=int, default=None)
    run.add_argument(
        "--crash-after-batch",
        type=int,
        default=None,
        help="test-only forced termination after the requested snapshot commit",
    )
    run.add_argument(
        "--no-resume",
        action="store_true",
        help="build from input rather than restoring a complete snapshot",
    )
    run.set_defaults(func=command_run)

    recovery = subparsers.add_parser("recovery", help="show the latest complete snapshot recovery point")
    recovery.add_argument("--output", required=True)
    recovery.set_defaults(func=command_show_recovery)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(json.dumps({"status": "error", "code": exc.code, "message": exc.message}, ensure_ascii=False))
        return 3
    except Exception as exc:
        print(json.dumps({
            "status": "error",
            "code": "E_RUNTIME",
            "exception_type": type(exc).__name__,
            "message": str(exc),
        }, ensure_ascii=False, sort_keys=True))
        return 1
