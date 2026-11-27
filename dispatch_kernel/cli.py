"""Command-line interface for the incident query reuse kernel."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .config import MATERIALIZE_THRESHOLD, MAX_MATERIALIZED
from .generator import SEED_DEFAULT
from .kernel import IncidentReuseKernel, InjectedInterruption


def _emit(value: Any, pretty: bool = True) -> None:
    if pretty:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dispatch-kernel",
        description="Deterministic incident query reuse kernel",
    )
    parser.add_argument("--db", default="dispatch.db", help="SQLite database path")
    parser.add_argument("--seed", type=int, default=SEED_DEFAULT)
    parser.add_argument("--reset", action="store_true", help="recreate from seed")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit one-line JSON instead of pretty JSON",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("init", help="initialize the base problem")
    subparsers.add_parser("status", help="show persistent state")
    subparsers.add_parser("plans", help="show normalized logical plans")
    subparsers.add_parser("candidates", help="show CSE candidates and decisions")
    subparsers.add_parser("results", help="show incremental query results")
    subparsers.add_parser("ledger", help="show current reuse ledger")
    subparsers.add_parser("updates", help="show update batch status")
    subparsers.add_parser("vectors", help="show version vectors")
    subparsers.add_parser("verify", help="compare all incremental results")
    subparsers.add_parser("demo", help="initialize, apply all updates, verify")
    subparsers.add_parser("run", help="alias for demo")

    normalize = subparsers.add_parser(
        "normalize",
        help="show normalized events and field provenance",
    )
    normalize.add_argument("--incident-id")

    lookup = subparsers.add_parser(
        "lookup",
        help="query reuse ledger by incident or time range",
    )
    lookup.add_argument("--incident-id")
    lookup.add_argument("--start-ms", type=int)
    lookup.add_argument("--end-ms", type=int)

    apply_parser = subparsers.add_parser(
        "apply",
        help="apply pending update batches",
    )
    apply_parser.add_argument("--count", type=int)
    apply_parser.add_argument(
        "--interrupt-after",
        type=int,
        help="commit this many batches then exit with code 75",
    )
    return parser


def _demo(kernel: IncidentReuseKernel) -> dict[str, Any]:
    before = kernel.status()
    summaries = kernel.apply_updates()
    report = kernel.latest_consistency_report()
    results = kernel.query_results()
    return {
        "status_before": before,
        "applied": summaries,
        "status_after": kernel.status(),
        "results": results,
        "ledger": kernel.ledger(),
        "result_summary": {
            query_id: {
                "row_count": result["row_count"],
                "sha256": result["blob_sha256"],
            }
            for query_id, result in sorted(results.items())
        },
        "consistency": {
            "consistent": report["consistent"] if report else None,
            "update_seq": report["update_seq"] if report else None,
            "difference_count": len(report["differences"]) if report else None,
        },
        "reuse": {
            "threshold": MATERIALIZE_THRESHOLD,
            "budget": MAX_MATERIALIZED,
            "candidate_count": len(kernel.candidate_report()),
            "materialized_count": sum(
                1 for item in kernel.candidate_report() if item["selected"]
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    command = args.command or "demo"
    pretty = not args.compact

    if command == "apply" and args.interrupt_after is not None:
        try:
            with IncidentReuseKernel(args.db, seed=args.seed, reset=args.reset) as kernel:
                summaries = kernel.apply_updates(
                    max_batches=args.count,
                    interrupt_after=args.interrupt_after,
                )
                _emit(
                    {"applied": summaries, "status": kernel.status()},
                    pretty,
                )
                return 0
        except InjectedInterruption as exc:
            with IncidentReuseKernel(args.db, seed=args.seed) as kernel:
                _emit(
                    {
                        "interrupted": True,
                        "message": str(exc),
                        "status": kernel.status(),
                    },
                    pretty,
                )
            return 75

    with IncidentReuseKernel(args.db, seed=args.seed, reset=args.reset) as kernel:
        if command == "init":
            _emit(kernel.status(), pretty)
        elif command == "status":
            _emit(kernel.status(), pretty)
        elif command == "plans":
            _emit(kernel.plan_report(), pretty)
        elif command == "candidates":
            _emit(kernel.candidate_report(), pretty)
        elif command == "results":
            _emit(kernel.query_results(), pretty)
        elif command == "ledger":
            _emit(kernel.ledger(), pretty)
        elif command == "updates":
            _emit(kernel.store.list_updates(), pretty)
        elif command == "vectors":
            _emit(kernel.version_vectors(), pretty)
        elif command == "verify":
            _emit(kernel.verify(), pretty)
        elif command == "normalize":
            _emit(kernel.normalization_report(args.incident_id), pretty)
        elif command == "lookup":
            if (
                args.incident_id is None
                and args.start_ms is None
                and args.end_ms is None
            ):
                parser.error("lookup requires --incident-id or a time range")
            _emit(
                kernel.ledger_lookup(
                    incident_id=args.incident_id,
                    start_ms=args.start_ms,
                    end_ms=args.end_ms,
                ),
                pretty,
            )
        elif command == "apply":
            _emit(
                {
                    "applied": kernel.apply_updates(max_batches=args.count),
                    "status": kernel.status(),
                },
                pretty,
            )
        elif command in {"demo", "run"}:
            _emit(_demo(kernel), pretty)
        else:
            parser.error(f"unknown command: {command}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
