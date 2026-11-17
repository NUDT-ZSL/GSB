"""Command-line interface for offline forensics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .digests import DEFAULT_ALGORITHMS
from .engine import EngineConfig, ForensicEngine
from .samplegen import generate_samples


def _load_hmac_keys(path: str | None) -> dict[str, str]:
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in payload.items()
    ):
        raise ValueError("hmac key file must be a JSON object mapping ids to string keys")
    return payload


def _run_command(args: argparse.Namespace) -> int:
    algorithms = tuple(
        item.strip() for item in args.algorithms.split(",") if item.strip()
    )
    config = EngineConfig(
        workers=args.workers,
        queue_limit=args.queue_limit,
        max_file_seconds=args.max_file_seconds,
        allowed_algorithms=algorithms or DEFAULT_ALGORITHMS,
        hmac_keys=_load_hmac_keys(args.hmac_key_file),
    )
    result = ForensicEngine(config).run(
        args.evidence_dir,
        args.baseline,
        args.report,
        args.graph,
        checkpoint_path=args.checkpoint,
    )
    summary = {
        "report": str(Path(args.report).resolve()),
        "graph": str(Path(args.graph).resolve()),
        "summary": result.report["summary"],
        "checkpoint": result.report["checkpoint"],
        "output_errors": [issue.to_dict() for issue in result.output_errors],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if not result.output_errors else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="config-forensics",
        description="Deterministic offline configuration drift forensics",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run adjudication over local evidence")
    run_parser.add_argument("--evidence-dir", required=True)
    run_parser.add_argument("--baseline", required=True)
    run_parser.add_argument("--report", required=True)
    run_parser.add_argument("--graph", required=True)
    run_parser.add_argument("--checkpoint")
    run_parser.add_argument("--workers", type=int, default=4)
    run_parser.add_argument("--queue-limit", type=int, default=16)
    run_parser.add_argument("--max-file-seconds", type=float, default=10.0)
    run_parser.add_argument(
        "--algorithms",
        default=",".join(DEFAULT_ALGORITHMS),
        help="comma-separated digest algorithm whitelist",
    )
    run_parser.add_argument(
        "--hmac-key-file",
        help="offline JSON object mapping hmac key ids to string keys",
    )
    run_parser.set_defaults(handler=_run_command)

    sample_parser = subparsers.add_parser(
        "sample", help="generate deterministic samples and truth manifest"
    )
    sample_parser.add_argument("--out", required=True)

    def sample_handler(args: argparse.Namespace) -> int:
        generate_samples(Path(args.out))
        print(str(Path(args.out).resolve()))
        return 0

    sample_parser.set_defaults(handler=sample_handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
