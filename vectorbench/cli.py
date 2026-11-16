"""Command-line entry point for local vectorization experiments."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Any, Sequence

from .errors import BenchError, ErrorCode
from .execution import FUSED, NAIVE
from .logging_utils import StructuredLogger
from .model import load_blocks, load_queries
from .session import ExperimentSession, open_snapshot

DEFAULT_DATA = "samples/data_blocks.json"
DEFAULT_QUERIES = "samples/queries.json"
DEFAULT_LOG = "logs/vectorbench.jsonl"


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False))


def _load_session(data_path: str, query_path: str, logger: StructuredLogger) -> ExperimentSession:
    blocks = load_blocks(data_path)
    if len(blocks) != 1:
        raise BenchError(
            ErrorCode.E_DATA,
            "demo runner expects exactly one data block",
            {"block_count": len(blocks)},
        )
    queries = load_queries(query_path, blocks[0])
    return ExperimentSession(blocks[0], queries, logger)


def _run(args: argparse.Namespace, logger: StructuredLogger) -> int:
    session = _load_session(args.data, args.queries, logger)
    if args.scale and args.scale > 1:
        session.block = session.block.scaled(args.scale)
        logger.event("data.scaled", {"factor": args.scale, "rows": session.block.row_count})
    candidates = tuple(sorted(set(args.batch_sizes))) if args.batch_sizes else None
    if candidates is not None:
        invalid = [size for size in candidates if size <= 0]
        if invalid:
            raise BenchError(
                ErrorCode.E_BATCH,
                "batch size candidates must be positive integers",
                {"invalid": invalid, "batch_sizes": list(candidates)},
            )
    session.evaluate_all(candidates)
    report = session.build_report()
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(report, handle, sort_keys=True, indent=2, ensure_ascii=False)
    if args.snapshot:
        session.snapshot(args.snapshot)
    _print_json(report)
    return 0


def _inject(args: argparse.Namespace, logger: StructuredLogger) -> int:
    fault_map = {
        "type": ("samples/faults/type_mismatch.json", "E_TYPE"),
        "nullmap": ("samples/faults/nullmap_misaligned.json", "E_NULLMAP"),
    }
    try:
        if args.fault == "batch":
            session = _load_session(args.data, args.queries, logger)
            logger.event("fault.injected", {"fault": "batch", "batch_size": 0})
            raise BenchError(
                ErrorCode.E_BATCH,
                "injected zero batch size",
                {"batch_size": 0, "block": session.block.name},
            )
        path, expected_code = fault_map[args.fault]
        logger.event("fault.injected", {"fault": args.fault, "path": path})
        blocks = load_blocks(path)
        load_queries(args.queries, blocks[0])
        return 0
    except BenchError as exc:
        expected = {"type": "E_TYPE", "nullmap": "E_NULLMAP", "batch": "E_BATCH"}[args.fault]
        payload = {"expected_code": expected, **exc.to_event()}
        logger.event("fault.reported", payload)
        _print_json(payload)
        if exc.code != expected:
            logger.event("fault.unexpected_code", {"actual": exc.code, "expected": expected})
            return 2
        return 1


def _replay(args: argparse.Namespace, logger: StructuredLogger) -> int:
    current = _load_session(args.data, args.queries, logger)
    restored, mode = open_snapshot(args.snapshot, current.block, current.queries, logger)
    report = restored.build_report()
    _print_json({"replacement_mode": mode, "report": report})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Vectorized expression experiment workbench")
    parser.add_argument("--log", default=DEFAULT_LOG, help="structured JSONL log path")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="evaluate queries on a local block")
    run.add_argument("--data", default=DEFAULT_DATA)
    run.add_argument("--queries", default=DEFAULT_QUERIES)
    run.add_argument("--scale", type=int, default=1, help="deterministic whole-block repeat factor")
    run.add_argument("--batch-sizes", type=int, nargs="*", default=[],
                     help="candidate positive batch sizes")
    run.add_argument("--report", default="")
    run.add_argument("--snapshot", default="")
    run.set_defaults(handler=_run)

    inject = sub.add_parser("inject", help="inject one of the supported deterministic faults")
    inject.add_argument("fault", choices=("type", "nullmap", "batch"))
    inject.add_argument("--data", default=DEFAULT_DATA)
    inject.add_argument("--queries", default=DEFAULT_QUERIES)
    inject.set_defaults(handler=_inject)

    replay = sub.add_parser("replay", help="reopen a snapshot and verify whole-state recovery")
    replay.add_argument("snapshot")
    replay.add_argument("--data", default=DEFAULT_DATA)
    replay.add_argument("--queries", default=DEFAULT_QUERIES)
    replay.set_defaults(handler=_replay)

    gui = sub.add_parser("gui", help="open the Tkinter workbench window")
    gui.add_argument("--data", default=DEFAULT_DATA)
    gui.add_argument("--queries", default=DEFAULT_QUERIES)
    gui.set_defaults(handler=_gui)
    return parser


def _gui(args: argparse.Namespace, logger: StructuredLogger) -> int:
    from .gui import launch_gui

    session = _load_session(args.data, args.queries, logger)
    session.evaluate_all()
    launch_gui(session)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = StructuredLogger(args.log)
    try:
        return args.handler(args, logger)
    except BenchError as exc:
        logger.event("error", exc.to_event())
        _print_json(exc.to_event())
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
