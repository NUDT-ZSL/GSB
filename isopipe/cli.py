from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .canonical import canonical_text
from .engine import PipelineEngine, run_summary
from .errors import ERROR_EXIT_CODES, PipelineError
from .verify import verify_pipeline


def _add_common(parser: argparse.ArgumentParser, *, mutation: bool) -> None:
    parser.add_argument("--workspace", default=".", help="workspace root")
    parser.add_argument("--manifest", required=True, help="relative task manifest path")
    parser.add_argument(
        "--state",
        default="var/pipeline/state.sqlite3",
        help="SQLite state path inside the workspace",
    )
    parser.add_argument(
        "--cache",
        default="var/pipeline/cache",
        help="cache directory inside the workspace",
    )
    if mutation:
        parser.add_argument(
            "--clock-start-ms",
            type=int,
            default=None,
            help="explicit logical clock origin",
        )
        parser.add_argument(
            "--fault-inject",
            default=None,
            help=argparse.SUPPRESS,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline.py",
        description="Deterministic permission-bounded local pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="execute a manifest")
    _add_common(run, mutation=True)
    resume = subparsers.add_parser("resume", help="resume persisted state")
    _add_common(resume, mutation=True)
    status = subparsers.add_parser("status", help="show persisted task status")
    _add_common(status, mutation=False)
    verify = subparsers.add_parser("verify", help="verify outputs and persisted state")
    _add_common(verify, mutation=False)
    return parser


def _engine(args: argparse.Namespace) -> PipelineEngine:
    workspace = Path(args.workspace).resolve()
    manifest = Path(args.manifest)
    if not manifest.is_absolute():
        manifest = workspace / manifest
    return PipelineEngine(
        workspace=workspace,
        manifest_path=manifest,
        state_db=Path(args.state),
        cache_dir=Path(args.cache),
        clock_start_ms=getattr(args, "clock_start_ms", None),
        fault_inject=getattr(args, "fault_inject", None),
    )


def _emit(value: Any) -> None:
    sys.stdout.write(canonical_text(value) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        engine = _engine(args)
        if args.command in ("run", "resume"):
            status = engine.execute(resume=args.command == "resume")
            summary = run_summary(status)
            _emit(summary)
            if summary["outcome"] == "failed":
                for task in summary["tasks"]:
                    code = task.get("error_code")
                    if code:
                        return ERROR_EXIT_CODES.get(code, 1)
            return 0
        if args.command == "status":
            _emit(engine.status())
            return 0
        if args.command == "verify":
            result = verify_pipeline(engine)
            _emit(result)
            if result["valid"]:
                return 0
            for item in result["errors"]:
                code = item.get("code")
                if code in ERROR_EXIT_CODES:
                    return ERROR_EXIT_CODES[code]
            return ERROR_EXIT_CODES["E_VERIFY_FAILED"]
        raise PipelineError("E_INTERNAL", f"unknown command: {args.command}")
    except PipelineError as exc:
        sys.stderr.write(
            canonical_text(
                {
                    "schema": "pipeline-error-v1",
                    "error": exc.as_dict(),
                    "exit_code": exc.exit_code,
                }
            )
            + "\n"
        )
        return exc.exit_code
    except Exception as exc:
        internal = PipelineError(
            "E_INTERNAL",
            f"unhandled failure: {exc}",
        )
        sys.stderr.write(
            canonical_text(
                {
                    "schema": "pipeline-error-v1",
                    "error": internal.as_dict(),
                    "exit_code": internal.exit_code,
                }
            )
            + "\n"
        )
        return internal.exit_code

