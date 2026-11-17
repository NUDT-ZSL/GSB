"""Command-line entry point for sample generation and auditing."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .atomic_io import atomic_write_json
from .config import AuditConfig
from .constants import ALGORITHM_WHITELIST_VERSION
from .engine import AuditEngine, report_error_lines, report_exit_code
from .errors import E_LOCK, E_PARAM, KlpConfigError, KlpError, KlpLockError
from .generator import generate_samples


class KlpArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{E_PARAM}: {message}\n")


def _timeout_value(raw: str) -> float | None:
    if raw.lower() in {"none", "off", "disabled"}:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number or 'none'") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("timeout must be non-negative")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = KlpArgumentParser(prog="klp-audit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser(
        "generate", help="write deterministic samples and truth manifests"
    )
    generate_parser.add_argument(
        "--output-dir",
        default="artifacts/generated",
        help="root directory for samples, truth, reports, and checkpoints",
    )

    audit_parser = subparsers.add_parser("audit", help="audit one KLP binary file")
    audit_parser.add_argument("input", help="path to a .klp file")
    audit_parser.add_argument(
        "--report",
        default="reports/audit-report.json",
        help="atomic JSON report path",
    )
    audit_parser.add_argument(
        "--checkpoint",
        default=None,
        help="checkpoint JSON path (disabled when omitted)",
    )
    audit_parser.add_argument(
        "--workspace",
        default=".",
        help="workspace whose single lock serializes checkpoint/report writes",
    )
    audit_parser.add_argument("--concurrency", type=int, default=4)
    audit_parser.add_argument(
        "--timeout",
        type=_timeout_value,
        default=30.0,
        help="real per-session wall-clock limit in seconds, or 'none'",
    )
    audit_parser.add_argument("--max-derived", type=int, default=50_000)
    audit_parser.add_argument("--scale-bytes", type=int, default=1_048_576)
    audit_parser.add_argument("--scale-derived", type=int, default=10_000)
    audit_parser.add_argument("--sample-ratio", type=float, default=0.25)
    audit_parser.add_argument("--chunk-size", type=int, default=8)
    audit_parser.add_argument("--checkpoint-interval", type=int, default=1)
    audit_parser.add_argument(
        "--algorithm-policy-version",
        default=ALGORITHM_WHITELIST_VERSION,
    )
    audit_parser.add_argument(
        "--stdout",
        action="store_true",
        help="also print the complete JSON report to stdout",
    )
    return parser


def _audit_from_args(args: argparse.Namespace) -> int:
    config = AuditConfig(
        concurrency=args.concurrency,
        session_timeout_seconds=args.timeout,
        max_derivations_per_session=args.max_derived,
        scale_bytes_threshold=args.scale_bytes,
        scale_derived_threshold=args.scale_derived,
        sampling_ratio=args.sample_ratio,
        sample_chunk_size=args.chunk_size,
        checkpoint_interval=args.checkpoint_interval,
        algorithm_policy_version=args.algorithm_policy_version,
    )
    config.validate()
    workspace = Path(args.workspace)
    engine = AuditEngine(
        config,
        checkpoint_path=args.checkpoint,
        workspace=workspace,
    )
    report = engine.audit_file(args.input)
    report_path = Path(args.report)
    atomic_write_json(
        report_path,
        report,
        lock_path=workspace / ".klp-workspace.lock",
    )

    error_lines = report_error_lines(report)
    for line in error_lines:
        print(line, file=sys.stderr)
    if args.stdout:
        print(
            json.dumps(
                report,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
        )
    conclusion = report["file"]["conclusion"]  # type: ignore[index]
    summary = report["summary"]  # type: ignore[index]
    print(
        f"{conclusion}: sessions={summary['sessions']} "  # type: ignore[index]
        f"failed={summary['failed']} degraded={summary['degraded_pass']} "  # type: ignore[index]
        f"rejected={summary['rejected_records']} report={report_path}",  # type: ignore[index]
        file=sys.stderr,
    )
    return report_exit_code(report)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            manifest_path = generate_samples(args.output_dir)
            print(str(manifest_path))
            return 0
        if args.command == "audit":
            return _audit_from_args(args)
        parser.error(f"unknown command {args.command}")
        return 2
    except KlpLockError as exc:
        print(f"{E_LOCK}: {exc.message}", file=sys.stderr)
        return 1
    except KlpConfigError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2
    except KlpError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"{E_PARAM}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

