"""Command line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .codes import BusinessCode, ExitCode
from .config import EngineConfig
from .generator import DEFAULT_SEED, generate_sample
from .pipeline import load_input_bundle, run_pipeline
from .storage import StorageError, atomic_write_json
from .verification import run_shuffled_batches


def _contains_business_error(value: Any) -> bool:
    if isinstance(value, dict):
        code = value.get("code")
        if isinstance(code, str) and code.startswith("E_"):
            return True
        return any(_contains_business_error(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_business_error(item) for item in value)
    return False


def _write_report(path: str | Path, report: dict[str, Any]) -> Path:
    return atomic_write_json(path, report)


def _config_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> EngineConfig:
    try:
        return EngineConfig(
            concurrency=args.concurrency,
            unit_timeout_ms=args.unit_timeout_ms,
            max_queue_size=args.max_queue_size,
            max_events_per_unit=args.max_events_per_unit,
            lock_timeout_ms=args.lock_timeout_ms,
        )
    except ValueError as exc:
        parser.error(str(exc))
        raise AssertionError("parser.error exits")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pdre", description="Process behavior policy decision engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate-sample", help="generate deterministic local sample")
    generate.add_argument("--out", default="samples/demo")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)

    run = subparsers.add_parser("run", help="run parsing, lineage, policy, and checkpoint pipeline")
    run.add_argument("--input-dir", default="samples/demo")
    run.add_argument("--report", default="reports/report.json")
    run.add_argument("--checkpoint-dir", default=".pdre-state/checkpoints")
    run.add_argument("--snapshot-dir", default=".pdre-state/snapshots")
    run.add_argument("--ledger", default=".pdre-state/ledger.jsonl")
    run.add_argument("--batch-size", type=int, default=8)
    run.add_argument("--concurrency", type=int, default=2)
    run.add_argument("--unit-timeout-ms", type=int, default=5_000)
    run.add_argument("--max-queue-size", type=int, default=256)
    run.add_argument("--max-events-per-unit", type=int, default=10_000)
    run.add_argument("--lock-timeout-ms", type=int, default=0)
    run.add_argument("--no-verify", action="store_true")
    run.add_argument("--fault", choices=["residue-then-kill-after-first-checkpoint"], default=None)

    verify = subparsers.add_parser("verify", help="verify order and batch invariance")
    verify.add_argument("--input-dir", default="samples/demo")
    verify.add_argument("--report", default="reports/verification.json")
    verify.add_argument("--iterations", type=int, default=20)
    verify.add_argument("--concurrency", type=int, default=2)
    verify.add_argument("--unit-timeout-ms", type=int, default=5_000)
    verify.add_argument("--max-queue-size", type=int, default=256)
    verify.add_argument("--max-events-per-unit", type=int, default=10_000)
    verify.add_argument("--lock-timeout-ms", type=int, default=0)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "generate-sample":
        paths = generate_sample(args.out, seed=args.seed)
        payload = {
            "status": "generated",
            "output_dir": args.out,
            "files": {name: str(path) for name, path in paths.items()},
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return ExitCode.SUCCESS

    if args.command == "verify":
        if args.iterations < 1:
            parser.error("--iterations must be >= 1")
        config = _config_from_args(args, parser)
        bundle = load_input_bundle(args.input_dir)
        result = run_shuffled_batches(
            bundle,
            iterations=args.iterations,
            config=config,
        )
        result["input_dir"] = args.input_dir
        _write_report(args.report, result)
        print(json.dumps({"report": args.report, "all_matched": result["all_matched"]}, ensure_ascii=False))
        return ExitCode.SUCCESS if result["all_matched"] else ExitCode.PARTIAL_FAILURE

    if args.command == "run":
        if args.batch_size < 1:
            parser.error("--batch-size must be >= 1")
        config = _config_from_args(args, parser)
        bundle = load_input_bundle(args.input_dir)
        try:
            report, _ = run_pipeline(
                bundle,
                checkpoint_dir=args.checkpoint_dir,
                snapshot_dir=args.snapshot_dir,
                ledger_path=args.ledger,
                config=config,
                batch_size=args.batch_size,
                verify=not args.no_verify,
                fault=args.fault,
            )
        except StorageError as exc:
            error_report = {
                "status": "storage_error",
                "errors": [exc.to_dict()],
            }
            _write_report(args.report, error_report)
            print(json.dumps({"report": args.report, "error": exc.code}, ensure_ascii=False))
            return ExitCode.PARTIAL_FAILURE

        report_path = _write_report(args.report, report)
        has_error = _contains_business_error(report)
        summary = report.get("summary", {})
        print(
            json.dumps(
                {
                    "report": str(report_path),
                    "semantic_digest": report.get("semantic_digest"),
                    "summary": summary,
                    "exit_code": ExitCode.PARTIAL_FAILURE if has_error else ExitCode.SUCCESS,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return ExitCode.PARTIAL_FAILURE if has_error else ExitCode.SUCCESS

    parser.error(f"unknown command: {args.command}")
    return ExitCode.PARAMETER_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
