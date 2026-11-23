from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from .errors import GraphStoreError
from .models import TraversalRequest
from .samples import DEFAULT_LOGICAL_CLOCK_MS
from .service import GraphStore
from .util import canonical_json


def _add_snapshot_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--v1", required=True, help="v1 snapshot JSON path")
    parser.add_argument("--v2", required=True, help="v2 snapshot directory")
    parser.add_argument(
        "--logical-clock-ms",
        type=int,
        default=DEFAULT_LOGICAL_CLOCK_MS,
        help="injected logical clock epoch for v1 relative seconds",
    )
    parser.add_argument(
        "--v1-clock-scale-ms",
        type=int,
        default=1000,
        help="milliseconds per v1 written second",
    )
    parser.add_argument(
        "--v1-rounding",
        choices=["half_up", "floor", "ceil"],
        default="half_up",
    )


def _add_state_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-db", required=True, help="SQLite state database")
    parser.add_argument("--lock-timeout", type=float, default=30.0)
    parser.add_argument(
        "--clock-rollback-tolerance-ms",
        type=int,
        default=0,
        help="allowed logical clock floor regression for replacement",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graphstore",
        description="Two-generation local graph store with budgeted parallel traversal",
    )
    parser.add_argument(
        "--pretty", action="store_true", help="pretty-print JSON output"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser(
        "load", help="parse, validate and build in memory without persistence"
    )
    _add_snapshot_args(load_parser)

    merge_parser = subparsers.add_parser(
        "merge", help="atomically build and publish a merged index"
    )
    _add_snapshot_args(merge_parser)
    _add_state_args(merge_parser)

    traverse_parser = subparsers.add_parser(
        "traverse", help="execute exact/prefix/reach traversal in bounded batches"
    )
    _add_state_args(traverse_parser)
    traverse_parser.add_argument(
        "--mode", choices=["reach", "exact", "prefix"], default="reach"
    )
    traverse_parser.add_argument("--start", action="append", default=[])
    traverse_parser.add_argument("--exact")
    traverse_parser.add_argument("--prefix")
    traverse_parser.add_argument("--max-depth", type=int, default=2)
    traverse_parser.add_argument("--edge-label", action="append", default=[])
    traverse_parser.add_argument("--min-edge-weight", type=float)
    traverse_parser.add_argument("--max-edge-weight", type=float)
    traverse_parser.add_argument("--max-parallel", type=int, default=2)
    traverse_parser.add_argument("--memory-budget", type=int, default=1000)
    traverse_parser.add_argument("--batch-size", type=int, default=2)

    resume_parser = subparsers.add_parser(
        "resume", help="replay only uncommitted batches of a run"
    )
    _add_state_args(resume_parser)
    resume_parser.add_argument("--run-id")

    status_parser = subparsers.add_parser(
        "status", help="print stable committed status"
    )
    status_parser.add_argument("--state-db", required=True)
    status_parser.add_argument("--run-id")

    verify_parser = subparsers.add_parser(
        "verify", help="check index rows, source rebuild and committed batch replay"
    )
    _add_state_args(verify_parser)
    verify_parser.add_argument("--run-id")
    verify_parser.add_argument("--v1")
    verify_parser.add_argument("--v2")
    verify_parser.add_argument("--logical-clock-ms", type=int)
    verify_parser.add_argument(
        "--v1-clock-scale-ms", type=int, default=1000
    )
    verify_parser.add_argument(
        "--v1-rounding", choices=["half_up", "floor", "ceil"], default="half_up"
    )
    return parser


def _store_from_args(args: argparse.Namespace) -> GraphStore:
    return GraphStore(
        args.state_db,
        lock_timeout=args.lock_timeout,
        clock_rollback_tolerance_ms=args.clock_rollback_tolerance_ms,
    )


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "load":
        store = GraphStore(":memory:-not-used")
        return store.load(
            v1_path=args.v1,
            v2_path=args.v2,
            logical_clock_ms=args.logical_clock_ms,
            v1_clock_scale_ms=args.v1_clock_scale_ms,
            v1_rounding=args.v1_rounding,
        )
    if args.command == "merge":
        return _store_from_args(args).merge(
            v1_path=args.v1,
            v2_path=args.v2,
            logical_clock_ms=args.logical_clock_ms,
            v1_clock_scale_ms=args.v1_clock_scale_ms,
            v1_rounding=args.v1_rounding,
        )
    if args.command == "traverse":
        request = TraversalRequest.create(
            mode=args.mode,
            starts=args.start,
            exact_node=args.exact,
            prefix=args.prefix,
            max_depth=args.max_depth,
            edge_labels=args.edge_label,
            min_edge_weight=args.min_edge_weight,
            max_edge_weight=args.max_edge_weight,
            max_parallel_tasks=args.max_parallel,
            memory_budget_entries=args.memory_budget,
            batch_size=args.batch_size,
        )
        return _store_from_args(args).traverse(request)
    if args.command == "resume":
        return _store_from_args(args).resume(args.run_id)
    if args.command == "status":
        return GraphStore(args.state_db).status(args.run_id)
    if args.command == "verify":
        store = _store_from_args(args)
        return store.verify(
            run_id=args.run_id,
            v1_path=args.v1,
            v2_path=args.v2,
            logical_clock_ms=args.logical_clock_ms,
            v1_clock_scale_ms=args.v1_clock_scale_ms,
            v1_rounding=args.v1_rounding,
        )
    raise AssertionError(args.command)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = _dispatch(args)
    except GraphStoreError as exc:
        print(canonical_json(exc.to_dict()), file=sys.stderr)
        return 1
    except FileExistsError as exc:
        print(
            canonical_json(
                {
                    "error": {
                        "code": "OUTPUT_ALREADY_EXISTS",
                        "message": str(exc),
                        "details": {},
                    }
                }
            ),
            file=sys.stderr,
        )
        return 1
    if args.pretty:
        print(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False))
    else:
        print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

