"""Command-line interface for the offline diagnostic tool."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional, Sequence

from . import codes
from .budget import LockConflict
from .engine import analyze_run
from .generate import generate_sample, verify_sample
from .models import AnalysisConfig


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tlsdiag",
        description="Offline deterministic TLS handshake diagnostics (no network access).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate a deterministic sample corpus")
    generate.add_argument("--out", required=True, help="output sample directory")
    generate.add_argument("--seed", type=int, default=20261125, help="deterministic seed")
    generate.add_argument("--json", action="store_true", help="print JSON result")

    verify = sub.add_parser("verify-sample", help="verify the sample _SYNC.json marker")
    verify.add_argument("--dir", required=True, help="generated sample directory")
    verify.add_argument("--json", action="store_true", help="print JSON result")

    analyze = sub.add_parser("analyze", help="analyze a JSONL handshake stream")
    analyze.add_argument("--handshake", required=True, help="handshake JSONL file")
    analyze.add_argument("--v1", help="v1 trust-anchor snapshot JSON")
    analyze.add_argument("--v2-shards", help="v2 trust-anchor shard directory")
    analyze.add_argument("--v2-db", help="v2 SQLite metadata database")
    analyze.add_argument("--out", help="output directory for report/graph/checkpoint")
    analyze.add_argument("--report", help="explicit report.json path")
    analyze.add_argument("--graph", help="explicit graph.json path")
    analyze.add_argument("--checkpoint", help="explicit checkpoint.json path")
    analyze.add_argument("--jobs", type=int, default=1, help="connection worker concurrency")
    analyze.add_argument(
        "--timeout-ms",
        type=int,
        default=5000,
        help="real wall-clock limit per connection (0 means an immediate timeout)",
    )
    analyze.add_argument(
        "--max-frags", type=int, default=10000, help="fragment cap per connection"
    )
    analyze.add_argument(
        "--max-expanded-bytes",
        type=int,
        default=16 * 1024 * 1024,
        help="expanded byte cap per connection",
    )
    analyze.add_argument(
        "--max-total-expanded-bytes",
        type=int,
        default=256 * 1024 * 1024,
        help="global expanded byte cap",
    )
    analyze.add_argument(
        "--lock-timeout-ms",
        type=int,
        default=0,
        help="wait for the output lock; 0 returns E_LOCK immediately",
    )
    analyze.add_argument(
        "--resume", action="store_true", help="resume from the last complete checkpoint"
    )
    analyze.add_argument(
        "--no-write", action="store_true", help="do not write report/graph files"
    )
    analyze.add_argument("--json", action="store_true", help="print the full report")
    return parser


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate":
            result = generate_sample(args.out, seed=args.seed)
            if args.json:
                _print_json(result)
            else:
                print(f"generated {result['file_count']} files under {result['out_dir']}")
                print(f"sync marker: {result['sync_marker']}")
            return 0

        if args.command == "verify-sample":
            result = verify_sample(args.dir)
            _print_json(result)
            return 0 if result.get("ok") else 1

        if args.command == "analyze":
            config = AnalysisConfig(
                jobs=args.jobs,
                timeout_ms=args.timeout_ms,
                max_frags_per_connection=args.max_frags,
                max_expanded_bytes_per_connection=args.max_expanded_bytes,
                max_total_expanded_bytes=args.max_total_expanded_bytes,
                lock_timeout_ms=args.lock_timeout_ms,
                resume=args.resume,
            )
            run = analyze_run(
                handshake_path=args.handshake,
                v1_path=args.v1,
                v2_shards=args.v2_shards,
                v2_db=args.v2_db,
                output_dir=args.out,
                report_path=args.report,
                graph_path=args.graph,
                checkpoint_path=args.checkpoint,
                config=config,
                resume=args.resume,
                write_outputs=not args.no_write,
            )
            if args.json:
                _print_json(run.report)
            else:
                summary = run.report["summary"]
                _print_json(
                    {
                        "status": "ok",
                        "report": (
                            str(args.report)
                            if args.report
                            else str(Path(args.out) / "report.json")
                            if args.out
                            else None
                        ),
                        "connections_total": summary["connections_total"],
                        "connections_trusted": summary["connections_trusted"],
                        "status_counts": summary["status_counts"],
                        "trust_conclusion_counts": summary["trust_conclusion_counts"],
                        "conclusion_digest": summary["conclusion_digest"],
                        "checkpoint": run.report["checkpoint"],
                    }
                )
            return 0
    except LockConflict as exc:
        _print_json(
            {
                "status": "error",
                "codes": [codes.E_LOCK],
                "message": exc.message,
                "detail": exc.detail,
            }
        )
        return 3
    except Exception as exc:  # CLI must produce parseable diagnostics
        _print_json(
            {
                "status": "error",
                "codes": [codes.E_INTERNAL],
                "message": str(exc),
                "error_type": type(exc).__name__,
            }
        )
        return 2
    return 2
