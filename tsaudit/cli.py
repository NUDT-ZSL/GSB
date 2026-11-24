"""Command line interface for the TSA audit component."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .codes import EXIT_ARGS, EXIT_OK, EXIT_PARTIAL, Code
from .engine import AuditEngine
from .errors import AuditError
from .generator import DEFAULT_SEED, generate_sample


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def arrival_order(value: str) -> tuple[str, str]:
    parts = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    if len(parts) != 2 or set(parts) != {"v1", "v2"}:
        raise argparse.ArgumentTypeError("--order must be a permutation of v1,v2")
    return parts  # type: ignore[return-value]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tsaudit",
        description="Offline deterministic code-signing timestamp audit ledger",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate fixed-seed samples")
    generate.add_argument("--out", default="samples")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)

    import_snapshot = subparsers.add_parser(
        "import-snapshot", aliases=["import"], help="import v1/v2 TSA trust snapshots"
    )
    import_snapshot.add_argument("--workspace", default="state")
    import_snapshot.add_argument("--samples")
    import_snapshot.add_argument("--v1")
    import_snapshot.add_argument("--v2-dir")
    import_snapshot.add_argument("--update")
    import_snapshot.add_argument("--clock-ms", type=int)
    import_snapshot.add_argument("--batch-id")
    import_snapshot.add_argument("--order", type=arrival_order, default=("v1", "v2"))

    anchor = subparsers.add_parser(
        "anchor", aliases=["adjudicate", "anchor-verdicts"],
        help="adjudicate token trust and artifact anchoring",
    )
    anchor.add_argument("--workspace", default="state")
    anchor.add_argument("--samples")
    anchor.add_argument("--order", type=arrival_order, default=("v1", "v2"))
    anchor.add_argument("--output")
    anchor.add_argument("--summary-only", action="store_true")

    export_report = subparsers.add_parser("export-report", help="atomically export JSON report")
    export_report.add_argument("--workspace", default="state")
    export_report.add_argument("--output", "-o", default="audit-report.json")

    check = subparsers.add_parser(
        "verify-checkpoint", aliases=["checkpoint"],
        help="verify checkpoint and optionally recover/rebuild",
    )
    check.add_argument("--workspace", default="state")
    check.add_argument("--recover", action="store_true")

    import_records = subparsers.add_parser("import-records", help="import a signature or token batch")
    import_records.add_argument("--workspace", default="state")
    import_records.add_argument("--signatures")
    import_records.add_argument("--tokens")
    import_records.add_argument("--batch-id")
    return parser


def _exit_for_report(report: dict[str, Any]) -> int:
    return EXIT_PARTIAL if report.get("rejections") else EXIT_OK


def _import_snapshot(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    engine = AuditEngine(args.workspace)
    if args.samples:
        report = engine.import_sample(args.samples, arrival_order=args.order)
        return report, _exit_for_report(report)
    if args.v1:
        if args.clock_ms is None:
            raise AuditError(Code.E_ARGS, "--clock-ms is required with --v1 unless --samples is used")
        report = engine.import_v1(args.v1, args.clock_ms, batch_id=args.batch_id)
    elif args.v2_dir:
        report = engine.import_v2(args.v2_dir, batch_id=args.batch_id)
    elif args.update:
        report = engine.import_update(args.update, batch_id=args.batch_id)
    else:
        raise AuditError(Code.E_ARGS, "one of --samples, --v1, --v2-dir or --update is required")
    return report, _exit_for_report(report)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else EXIT_ARGS

    try:
        if args.command == "generate":
            result = generate_sample(Path(args.out), seed=args.seed)
            print_json(result)
            return EXIT_OK

        if args.command in {"import-snapshot", "import"}:
            payload, exit_code = _import_snapshot(args)
            print_json({
                "status": "partial" if payload.get("rejections") else "imported",
                "batch_id": payload.get("batch_id"),
                "checkpoint_seq": payload.get("checkpoint_seq"),
                "rejection_count": len(payload.get("rejections", [])),
                "summary": payload.get("summary"),
            })
            return exit_code

        if args.command == "import-records":
            engine = AuditEngine(args.workspace)
            if args.signatures:
                report = engine.import_signatures(args.signatures, batch_id=args.batch_id)
            elif args.tokens:
                report = engine.import_tokens(args.tokens, batch_id=args.batch_id)
            else:
                raise AuditError(Code.E_ARGS, "--signatures or --tokens is required")
            print_json({"status": "partial" if report.get("rejections") else "imported",
                        "batch_id": report.get("batch_id"),
                        "rejection_count": len(report.get("rejections", []))})
            return _exit_for_report(report)

        if args.command in {"anchor", "adjudicate", "anchor-verdicts"}:
            engine = AuditEngine(args.workspace)
            if args.samples:
                report = engine.adjudicate_sample(args.samples, arrival_order=args.order)
            else:
                report = engine.anchor()
            if args.output:
                engine.export_report(args.output)
            print_json(report["summary"] if args.summary_only else report)
            return _exit_for_report(report)

        if args.command == "export-report":
            engine = AuditEngine(args.workspace)
            report = engine.export_report(args.output)
            print_json({
                "status": "exported",
                "output": str(Path(args.output)),
                "artifact_count": len(report.get("artifacts", [])),
                "ledger_digest": report.get("ledger_digest"),
            })
            return _exit_for_report(report)

        if args.command in {"verify-checkpoint", "checkpoint"}:
            engine = AuditEngine(args.workspace)
            result = engine.verify_checkpoint(recover=bool(args.recover))
            print_json(result)
            if result.get("status") in {"consistent", "empty", "recovered", "rebuilt"} and \
                    result.get("ledger_consistent", True):
                return EXIT_OK
            return EXIT_PARTIAL

        raise AuditError(Code.E_ARGS, f"unknown command: {args.command}")
    except AuditError as exc:
        print_json({"status": "error", **exc.to_dict()})
        return EXIT_ARGS if exc.code == Code.E_ARGS else EXIT_PARTIAL
    except Exception as exc:
        print_json({
            "status": "error",
            "code": "E_INTERNAL",
            "message": str(exc),
            "details": {"exception_type": type(exc).__name__},
        })
        return EXIT_PARTIAL


if __name__ == "__main__":
    raise SystemExit(main())
