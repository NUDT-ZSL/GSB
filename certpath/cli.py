"""Command-line interface for the offline certificate-path engine."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .engine import CertPathEngine
from .errors import E_ARGS, CertPathError
from .generator import DEFAULT_SEED, generate_sample


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _arrival_order(value: str) -> tuple[str, str]:
    parts = tuple(item.strip() for item in value.split(",") if item.strip())
    if len(parts) != 2 or set(parts) != {"v1", "v2"}:
        raise argparse.ArgumentTypeError(
            "--order must be a permutation of v1,v2"
        )
    return parts  # type: ignore[return-value]


def _rejection_exit(payload: dict[str, Any]) -> int:
    return 1 if payload.get("rejections") else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="certpath",
        description=(
            "Offline deterministic cross-signed certificate path adjudicator"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate", help="generate the fixed-seed local sample set"
    )
    generate.add_argument("--out", default="samples")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)

    import_cmd = subparsers.add_parser(
        "import-snapshot", help="parse and atomically import v1/v2 snapshots"
    )
    import_cmd.add_argument("--samples", required=True)
    import_cmd.add_argument("--workspace", default="state")
    import_cmd.add_argument(
        "--order",
        type=_arrival_order,
        default=("v1", "v2"),
        help="snapshot arrival order, e.g. v2,v1",
    )

    verify = subparsers.add_parser(
        "verify-chain", help="adjudicate all chain declarations"
    )
    verify.add_argument("--samples", required=True)
    verify.add_argument("--workspace", default="state")
    verify.add_argument(
        "--order",
        type=_arrival_order,
        default=("v1", "v2"),
        help="snapshot arrival order, e.g. v2,v1",
    )
    verify.add_argument(
        "--summary-only",
        action="store_true",
        help="print the report summary instead of the full JSON report",
    )

    query = subparsers.add_parser(
        "query-verdict", help="query one or all persisted chain verdicts"
    )
    query.add_argument("--workspace", default="state")
    query.add_argument("--chain-id")

    export = subparsers.add_parser(
        "export-report", help="atomically export the diagnostic JSON report"
    )
    export.add_argument("--workspace", default="state")
    export.add_argument("--output", default="diagnostic-report.json")

    checkpoint = subparsers.add_parser(
        "verify-checkpoint",
        help="verify or recover from the last complete checkpoint",
    )
    checkpoint.add_argument("--workspace", default="state")
    checkpoint.add_argument(
        "--recover",
        action="store_true",
        help="restore the last complete checkpoint when residue is detected",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        if args.command == "generate":
            _print_json(generate_sample(args.out, args.seed))
            return 0
        if args.command == "import-snapshot":
            engine = CertPathEngine(args.workspace)
            payload = engine.import_sample(
                args.samples, arrival_order=args.order
            )
            _print_json(payload)
            return _rejection_exit(payload)
        if args.command == "verify-chain":
            engine = CertPathEngine(args.workspace)
            report = engine.adjudicate(
                args.samples, arrival_order=args.order
            )
            _print_json(report["summary"] if args.summary_only else report)
            return 1 if report["rejections"] else 0
        if args.command == "query-verdict":
            engine = CertPathEngine(args.workspace)
            _print_json(engine.query(args.chain_id))
            return 0
        if args.command == "export-report":
            engine = CertPathEngine(args.workspace)
            report = engine.export_report(Path(args.output))
            _print_json(
                {
                    "format": "certpath.export-result",
                    "version": 1,
                    "output": str(Path(args.output)),
                    "chains": len(report.get("chains", [])),
                    "round_key": report.get("round_key"),
                }
            )
            return 0
        if args.command == "verify-checkpoint":
            engine = CertPathEngine(args.workspace)
            result = engine.verify_checkpoint(recover=args.recover)
            _print_json(result)
            healthy = result.get("status") in {"consistent", "empty", "recovered"}
            return 0 if result.get("ledger_consistent") and healthy else 1
        raise CertPathError(E_ARGS, f"unknown command: {args.command}")
    except CertPathError as exc:
        _print_json({"status": "error", **exc.to_dict()})
        return 1
    except Exception as exc:
        _print_json(
            {
                "status": "error",
                "code": "E_INTERNAL",
                "message": str(exc),
                "details": {"exception_type": type(exc).__name__},
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
