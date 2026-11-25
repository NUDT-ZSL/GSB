from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from .cache import CacheStore
from .checkpoint import SimulatedHardStop, run_checkpointed_batches
from .errors import E_INTERNAL_ERROR, E_PARAM_INVALID, EngineError, param_error
from .generator import prepare_workspace
from .query import QueryService
from .report import _checkpoint_brief, run_canonical
from .sample import read_rows_csv
from .snapshot import load_v2, migrate_v1_to_v2, migrate_v2_to_v1
from .timing import LogicalClock, PerfClock
from .util import atomic_write_json, canonical_json


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="feature-diag",
        description="Deterministic offline tabular feature encoding and selection diagnostics",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate the fixed-seed sample and v1/v2 snapshots")
    generate.add_argument("--root", type=Path, default=Path.cwd())
    generate.add_argument("--seed", type=int, default=20261126)
    generate.add_argument("--count", type=int, default=32)

    run = subparsers.add_parser("run", help="run the complete deterministic acceptance workflow")
    run.add_argument("--root", type=Path, default=Path.cwd())
    run.add_argument("--seed", type=int, default=20261126)
    run.add_argument("--query-ids", default=",".join(["r0001", "r0009", "r0021", "r0030", "r0032"]))
    run.add_argument("--report", type=Path, default=None)
    run.add_argument("--telemetry", type=Path, default=None)
    run.add_argument("--no-generate", action="store_true")

    verify = subparsers.add_parser("verify", help="run twice and compare canonical report bytes")
    verify.add_argument("--root", type=Path, default=Path.cwd())
    verify.add_argument("--seed", type=int, default=20261126)

    query = subparsers.add_parser("query", help="query one committed row by row_id")
    query.add_argument("--root", type=Path, default=Path.cwd())
    query.add_argument("--row-id", required=True)
    query.add_argument("--timeout-ms", type=float, default=None)
    query.add_argument("--clock", choices=["actual", "logical"], default="actual")
    query.add_argument("--selection-key", default=None)
    query.add_argument("--output", type=Path, default=None)

    incremental = subparsers.add_parser("incremental", help="run the canonical incremental-update suite")
    incremental.add_argument("--root", type=Path, default=Path.cwd())
    incremental.add_argument("--report", type=Path, default=None)

    diagnose = subparsers.add_parser("diagnose", help="run the canonical injected-validation matrix")
    diagnose.add_argument("--root", type=Path, default=Path.cwd())
    diagnose.add_argument("--report", type=Path, default=None)

    migrate = subparsers.add_parser("migrate", help="migrate a snapshot between v1 and v2")
    migrate.add_argument("--root", type=Path, default=Path.cwd())
    migrate.add_argument("--direction", choices=["v1-to-v2", "v2-to-v1"], required=True)
    migrate.add_argument("--input", type=Path, required=True)
    migrate.add_argument("--output", type=Path, required=True)
    migrate.add_argument("--target-version", type=int, default=None)
    migrate.add_argument("--checksum", default=None)

    checkpoint = subparsers.add_parser("checkpoint", help="run or resume checkpointed batch execution")
    checkpoint.add_argument("--root", type=Path, default=Path.cwd())
    checkpoint.add_argument("--reset", action="store_true")
    checkpoint.add_argument("--resume", action="store_true")
    checkpoint.add_argument("--batch-size", type=int, default=12)
    checkpoint.add_argument("--hard-stop-after", type=int, default=None)
    checkpoint.add_argument("--graceful-after", type=int, default=None)
    checkpoint.add_argument("--checkpoint-db", type=Path, default=None)
    checkpoint.add_argument("--report", type=Path, default=None)

    return parser


def _print_json(value: Any, output: Path | None = None) -> None:
    text = canonical_json(value, newline=True)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def _load_workspace(root: Path) -> tuple[Any, list[dict[str, Any]]]:
    v2 = load_v2(root / "configs" / "v2", require_checksum=True)
    rows = read_rows_csv(root / "data" / "sample.csv", v2)
    return v2, rows


def _cmd_generate(args: argparse.Namespace) -> int:
    result = prepare_workspace(args.root, seed=args.seed, count=args.count)
    _print_json({"status": "ok", "workspace": result})
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    query_ids = [item.strip() for item in args.query_ids.split(",") if item.strip()]
    if not query_ids:
        raise param_error("at least one query row_id is required", field="query_ids")
    root = args.root.resolve()
    report_path = args.report.resolve() if args.report else None
    telemetry_path = args.telemetry.resolve() if args.telemetry else None
    _, info = run_canonical(
        root,
        seed=args.seed,
        query_ids=query_ids,
        report_path=report_path,
        telemetry_path=telemetry_path,
        generate_if_missing=not args.no_generate,
    )
    _print_json(
        {
            "status": "ok",
            "report_path": info["report_path"],
            "telemetry_path": info["telemetry_path"],
            "report_sha256": info["report_sha256"],
            "row_count": info["row_count"],
            "final_selection_key": info["final_selection_key"],
        }
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    report_path = root / "artifacts" / "verify_report.json"
    first_info = run_canonical(root, seed=args.seed, report_path=report_path)[1]
    first_bytes = report_path.read_bytes()
    second_info = run_canonical(root, seed=args.seed, report_path=report_path)[1]
    second_bytes = report_path.read_bytes()
    _print_json(
        {
            "status": "ok" if first_bytes == second_bytes else "failed",
            "reports_byte_identical": first_bytes == second_bytes,
            "first_report_sha256": first_info["report_sha256"],
            "second_report_sha256": second_info["report_sha256"],
            "report_bytes": len(second_bytes),
        }
    )
    return 0 if first_bytes == second_bytes else 1


def _cmd_query(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    config, _ = _load_workspace(root)
    cache = CacheStore(root / "work" / "run-cache.sqlite", config.limits["max_cache_entries"])
    clock = PerfClock() if args.clock == "actual" else LogicalClock()
    response = QueryService(cache).query_row(
        args.row_id,
        timeout_ms=args.timeout_ms,
        clock=clock,
        selection_key=args.selection_key,
    )
    _print_json({"status": "ok", "query": response}, args.output)
    return 0


def _cmd_incremental(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    report_path = args.report.resolve() if args.report else root / "artifacts" / "incremental_report.json"
    report, info = run_canonical(root, report_path=report_path)
    _print_json(
        {
            "status": "ok",
            "report_sha256": info["report_sha256"],
            "incremental": report["incremental"],
            "cache": report["cache"],
            "summary": report["summary"],
        }
    )
    return 0


def _cmd_diagnose(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    report_path = args.report.resolve() if args.report else root / "artifacts" / "diagnostic_report.json"
    report, info = run_canonical(root, report_path=report_path)
    _print_json(
        {
            "status": "ok",
            "report_sha256": info["report_sha256"],
            "diagnostics": report["diagnostics"],
            "business_codes": report["business_codes"],
            "degradations": report["degradations"],
        }
    )
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    input_path = args.input if args.input.is_absolute() else args.root / args.input
    output_path = args.output if args.output.is_absolute() else args.root / args.output
    if args.direction == "v1-to-v2":
        config, result = migrate_v1_to_v2(
            input_path,
            output_path,
            target_version=args.target_version,
            checksum=args.checksum,
        )
    else:
        config, result = migrate_v2_to_v1(
            input_path,
            output_path,
            target_version=args.target_version,
            checksum=args.checksum,
        )
    _print_json(
        {
            "status": "ok",
            "direction": args.direction,
            "snapshot_version": config.snapshot_version,
            "semantic_digest": config.semantic_digest,
            "completion_sources": config.completion_sources,
            "result": result,
        }
    )
    return 0


def _cmd_checkpoint(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    config, rows = _load_workspace(root)
    checkpoint_path = (
        args.checkpoint_db.resolve()
        if args.checkpoint_db
        else root / "work" / "checkpoint.sqlite"
    )
    if args.reset and args.resume:
        raise param_error("--reset and --resume are mutually exclusive", field="checkpoint_mode")
    if args.hard_stop_after is not None and args.hard_stop_after < 1:
        raise param_error("--hard-stop-after must be positive", field="hard_stop_after")
    if args.graceful_after is not None and args.graceful_after < 1:
        raise param_error("--graceful-after must be positive", field="graceful_after")

    graceful_flag = {"requested": False}

    def request_graceful(signum: int, frame: Any) -> None:
        graceful_flag["requested"] = True

    old_int = signal.signal(signal.SIGINT, request_graceful)
    old_term = signal.signal(signal.SIGTERM, request_graceful)
    try:
        result = run_checkpointed_batches(
            rows,
            config,
            checkpoint_path,
            requested_batch_size=args.batch_size,
            reset=args.reset,
            hard_stop_after_batches=args.hard_stop_after,
            graceful_stop_after_batches=args.graceful_after,
            graceful_requested=lambda: graceful_flag["requested"],
        )
    except SimulatedHardStop as stop:
        sys.stdout.write(
            canonical_json(
                {
                    "status": "forced_termination_simulated",
                    "checkpoint_version": stop.checkpoint_version,
                    "report_generated": False,
                },
                newline=True,
            )
        )
        sys.stdout.flush()
        os._exit(137)
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)

    computation = result.get("computation")
    output = _checkpoint_brief(result)
    if computation is not None:
        output["batch"] = computation.summary()
    report_file = (
        args.report.resolve()
        if args.report
        else root / "artifacts" / "checkpoint_report.json"
    )
    atomic_write_json(report_file, output)
    _print_json(
        {
            "status": result["status"],
            "resume_reason": result["resume_reason"],
            "checkpoint_version": result["checkpoint_version"],
            "report_path": str(report_file),
            "selection_available": result["selection_available"],
            "rows_reused_from_checkpoint": result["rows_reused_from_checkpoint"],
            "recomputed_row_ids": result["recomputed_row_ids"],
            "degradations": result["degradations"],
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            return _cmd_generate(args)
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "verify":
            return _cmd_verify(args)
        if args.command == "query":
            return _cmd_query(args)
        if args.command == "incremental":
            return _cmd_incremental(args)
        if args.command == "diagnose":
            return _cmd_diagnose(args)
        if args.command == "migrate":
            return _cmd_migrate(args)
        if args.command == "checkpoint":
            return _cmd_checkpoint(args)
        raise param_error("unknown command", field="command", value=args.command)
    except EngineError as error:
        _print_json({"status": "error", "error": error.to_dict()})
        return 1
    except Exception as error:
        wrapped = EngineError(
            E_INTERNAL_ERROR,
            "unhandled exception reached the CLI boundary",
            {"exception_type": type(error).__name__, "exception": str(error)},
        )
        _print_json({"status": "error", "error": wrapped.to_dict()})
        return 2
