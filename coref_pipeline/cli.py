"""Command-line entry points."""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from typing import Any

from .canonical import pretty_json
from .config import RulesConfig
from .errors import E_INTERNAL, PipelineError
from .generator import generate_sample
from .pipeline import failure_report, run_pipeline, write_report
from .verify import verify_report


def _add_run_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("run", help="ingest batches and build coreference chains")
    parser.add_argument("--sample-dir", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--batch-order",
        default=None,
        help="comma-separated batch numbers; execution is always ascending",
    )
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--snapshot-dir", default=None)
    parser.add_argument("--cache-trace", action="store_true")


def _add_generate_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("generate", help="generate deterministic samples")
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=20261124)
    parser.add_argument("--events", type=int, default=14)
    parser.add_argument("--batches", type=int, default=4)
    parser.add_argument("--force", action="store_true")


def _add_verify_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("verify", help="independently verify a JSON report")
    parser.add_argument("--report", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--output", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coref-pipeline")
    parser.add_argument("--version", action="version", version="0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_run_parser(subparsers)
    _add_generate_parser(subparsers)
    _add_verify_parser(subparsers)
    return parser


def _run(args: argparse.Namespace) -> int:
    try:
        report = run_pipeline(
            sample_dir=args.sample_dir,
            config_path=args.config,
            output_path=args.output,
            batch_order=args.batch_order,
            cache_dir=args.cache_dir,
            snapshot_dir=args.snapshot_dir,
            include_cache_trace=args.cache_trace,
        )
        print(
            pretty_json(
                {
                    "status": report["status"],
                    "output": str(Path(args.output)),
                    "chains": len(report["chains"]),
                    "evidence": report["evidence"]["count"],
                    "consistency": report["consistency"]["ok"],
                    "verification": report["verification"]["ok"],
                }
            ),
            end="",
        )
        return 0
    except PipelineError as exc:
        report = failure_report(
            exc,
            sample_dir=args.sample_dir,
            batch_order=args.batch_order,
        )
        write_report(args.output, report)
        print(pretty_json({"status": "error", "error": exc.to_dict()}), end="", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        error = PipelineError(
            code=E_INTERNAL,
            stage="internal",
            field="$",
            message=f"未预期异常: {type(exc).__name__}: {exc}",
            details={"traceback": traceback.format_exc().splitlines()},
        )
        report = failure_report(error, sample_dir=args.sample_dir)
        write_report(args.output, report)
        print(pretty_json({"status": "error", "error": error.to_dict()}), end="", file=sys.stderr)
        return 3


def _generate(args: argparse.Namespace) -> int:
    manifest = generate_sample(
        args.out,
        seed=args.seed,
        event_count=args.events,
        batch_count=args.batches,
        force=args.force,
    )
    print(pretty_json(manifest), end="")
    return 0


def _verify(args: argparse.Namespace) -> int:
    try:
        import json

        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        config = RulesConfig.load(args.config) if args.config else None
        result = verify_report(report, config=config)
        payload: dict[str, Any] = {
            "schema_version": "verification-report/v1",
            "report": str(Path(args.report)),
            **result,
        }
        if args.output:
            write_report(args.output, payload)
        print(pretty_json(payload), end="")
        return 0 if result["ok"] else 2
    except PipelineError as exc:
        payload = {
            "schema_version": "verification-report/v1",
            "ok": False,
            "errors": [exc.to_dict()],
            "business_codes": [exc.code],
        }
        if args.output:
            write_report(args.output, payload)
        print(pretty_json(payload), end="")
        return 2
    except (OSError, ValueError) as exc:
        error = PipelineError(
            code=E_INTERNAL,
            stage="verify",
            field="$",
            message=f"无法读取报告: {exc}",
        )
        payload = {
            "schema_version": "verification-report/v1",
            "ok": False,
            "errors": [error.to_dict()],
            "business_codes": [error.code],
        }
        if args.output:
            write_report(args.output, payload)
        print(pretty_json(payload), end="")
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    if args.command == "generate":
        return _generate(args)
    if args.command == "verify":
        return _verify(args)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

