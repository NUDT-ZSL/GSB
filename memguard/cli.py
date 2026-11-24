"""Deterministic command-line interface for the memory-region guard."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .constants import GuardError, EXIT_OK, EXIT_PARTIAL
from .locking import ExclusiveFileLock
from .samplegen import SEED, generate_sample
from .service import (
    run_disposition,
    run_import,
    run_query,
    run_report,
    run_rollback,
    run_verify,
)


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


def _add_common_inputs(parser) -> None:
    parser.add_argument("--workdir", default=".", help="workspace root")
    parser.add_argument("--sample-dir", default=None)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--report-dir", default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--events", type=Path, default=None)
    parser.add_argument("--regions", type=Path, default=None)
    parser.add_argument("--v1", type=Path, default=None)
    parser.add_argument("--v2-manifest", type=Path, default=None)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m memguard",
        description="Offline memory-mapping attribute guard and rollback ledger.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate-sample", aliases=["sample"])
    generate.add_argument("--workdir", default=".")
    generate.add_argument("--out", default=None)
    generate.add_argument("--seed", type=int, default=SEED)

    imported = subparsers.add_parser("import", aliases=["import-inputs"])
    _add_common_inputs(imported)

    process = subparsers.add_parser("process", aliases=["dispose", "run"])
    _add_common_inputs(process)
    process.add_argument("--rebuild", action="store_true")

    query = subparsers.add_parser("query", aliases=["query-region"])
    query.add_argument("--workdir", default=".")
    query.add_argument("--state-dir", default=None)
    query.add_argument("--endpoint-id", default=None)
    query.add_argument("--process-guid", default=None)
    query.add_argument("--region-id", default=None)

    report = subparsers.add_parser("report", aliases=["export", "export-report"])
    _add_common_inputs(report)
    report.add_argument("--out", type=Path, default=None)

    verify = subparsers.add_parser(
        "verify-checkpoint", aliases=["verify", "checkpoint"]
    )
    _add_common_inputs(verify)
    verify.add_argument("--full-replay", action="store_true")

    rollback = subparsers.add_parser("rollback", aliases=["rollback-closure"])
    _add_common_inputs(rollback)
    rollback.add_argument("--credential-id", required=True)
    rollback.add_argument(
        "--scope", choices=("module", "process", "region"), default="module"
    )

    lock_hold = subparsers.add_parser("lock-hold")
    lock_hold.add_argument("--workdir", default=".")
    lock_hold.add_argument("--state-dir", default=None)
    lock_hold.add_argument("--seconds", type=float, default=5.0)

    return parser


def _resolve_common_paths(args) -> None:
    workdir = Path(getattr(args, "workdir", ".")).resolve()
    if getattr(args, "sample_dir", None) is None:
        args.sample_dir = workdir / "samples" / "seed-400"
    else:
        args.sample_dir = Path(args.sample_dir)
        if not args.sample_dir.is_absolute():
            args.sample_dir = workdir / args.sample_dir
    if getattr(args, "state_dir", None) is None:
        args.state_dir = workdir / "state"
    else:
        args.state_dir = Path(args.state_dir)
        if not args.state_dir.is_absolute():
            args.state_dir = workdir / args.state_dir
    if getattr(args, "report_dir", None) is None:
        args.report_dir = workdir / "reports"
    else:
        args.report_dir = Path(args.report_dir)
        if not args.report_dir.is_absolute():
            args.report_dir = workdir / args.report_dir


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in {"generate-sample", "sample"}:
        workdir = Path(args.workdir).resolve()
        out = Path(args.out) if args.out else workdir / "samples" / "seed-400"
        if not out.is_absolute():
            out = workdir / out
        truth = generate_sample(out, seed=args.seed)
        emit(
            {
                "command": "generate-sample",
                "out_dir": str(out),
                "seed": args.seed,
                "truth_path": str(out / "truth.json"),
                "truth_records": len(truth["records"]),
                "expected_credential_count": truth["expected_credential_count"],
            }
        )
        return EXIT_OK

    if args.command == "lock-hold":
        workdir = Path(args.workdir).resolve()
        state_dir = (
            Path(args.state_dir)
            if args.state_dir
            else workdir / "state"
        )
        if not state_dir.is_absolute():
            state_dir = workdir / state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        with ExclusiveFileLock(state_dir / "guard.lock"):
            emit({"command": "lock-hold", "status": "locked", "state_dir": str(state_dir)})
            time.sleep(args.seconds)
        return EXIT_OK

    _resolve_common_paths(args)

    try:
        if args.command == "import":
            result = run_import(args)
        elif args.command in {"process", "dispose", "run"}:
            result = run_disposition(args)
        elif args.command in {"query", "query-region"}:
            result = run_query(args)
        elif args.command in {"report", "export", "export-report"}:
            result = run_report(args)
        elif args.command in {"verify-checkpoint", "verify", "checkpoint"}:
            result = run_verify(args)
        elif args.command in {"rollback", "rollback-closure"}:
            result = run_rollback(args)
        else:  # pragma: no cover - argparse enforces this
            parser.error(f"unknown command: {args.command}")
            return 2
    except GuardError as exc:
        payload = exc.to_dict()
        payload["command"] = args.command
        emit(payload)
        return exc.exit_code
    except Exception as exc:
        emit(
            {
                "code": "E_INTERNAL",
                "command": args.command,
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        )
        return EXIT_PARTIAL

    emit(result)
    has_partial = bool(result.get("partial")) or bool(result.get("rejections"))
    return EXIT_PARTIAL if has_partial else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
