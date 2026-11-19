"""Command-line interface for the local cache manager."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from . import __version__
from .engine import (
    execute_judge,
    execute_merge,
    execute_replace,
    execute_resume,
    execute_verify,
)
from .errors import (
    CACHE_INTERNAL_ERROR,
    CACHE_MANIFEST_INVALID,
    CacheError,
)
from .loader import (
    dataset_input_hash,
    load_dataset,
    load_dataset_for_clock,
    parse_package,
)
from .pipeline import load_pipelines
from .store import StateStore
from .util import pretty_json


class CacheArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CacheError(
            CACHE_MANIFEST_INVALID,
            "invalid command-line arguments",
            fields={"argument_error": message},
        )


def _nonnegative_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return value


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--v1", action="append", default=[], metavar="MANIFEST")
    parser.add_argument("--v2", action="append", default=[], metavar="META_DB")


def build_parser() -> argparse.ArgumentParser:
    parser = CacheArgumentParser(
        prog="cacheman",
        description="Offline CI/CD artifact cache manager for cache-v1 and cache-v2.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--state-dir",
        default=".cacheman",
        help="directory containing index.sqlite3 and the writer lock",
    )
    parser.add_argument(
        "--clock-tolerance-ms",
        type=_nonnegative_int,
        default=0,
        help="permitted logical clock rollback in milliseconds",
    )
    parser.add_argument(
        "--lock-timeout-seconds",
        type=_positive_float,
        default=15.0,
        help="file-lock acquisition timeout",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate sources without writing state")
    _add_source_arguments(load_parser)
    load_parser.add_argument("--now-ms", type=_nonnegative_int, default=None)

    merge_parser = subparsers.add_parser("merge", help="merge sources into a new index snapshot")
    _add_source_arguments(merge_parser)
    merge_parser.add_argument("--now-ms", type=_nonnegative_int, default=None)
    merge_parser.add_argument(
        "--fault-point",
        choices=("after_prepare", "after_stage"),
        default=None,
        help=argparse.SUPPRESS,
    )

    judge_parser = subparsers.add_parser("judge", help="judge reuse along pipeline dependencies")
    judge_parser.add_argument("--pipeline", action="append", required=True, metavar="JSON")
    judge_parser.add_argument("--now-ms", type=_nonnegative_int, default=None)

    replace_parser = subparsers.add_parser("replace", help="publish a cache package replacement")
    replace_parser.add_argument("--package", required=True, metavar="PACKAGE")
    replace_parser.add_argument("--pipeline", action="append", default=[], metavar="JSON")
    replace_parser.add_argument("--now-ms", type=_nonnegative_int, default=None)
    replace_parser.add_argument(
        "--fault-point",
        choices=("after_prepare", "after_stage"),
        default=None,
        help=argparse.SUPPRESS,
    )

    subparsers.add_parser("resume", help="recover prepared or staged operations")
    subparsers.add_parser("status", help="show the committed index snapshot")

    verify_parser = subparsers.add_parser("verify", help="verify state, artifact bytes, and sources")
    _add_source_arguments(verify_parser)
    verify_parser.add_argument("--pipeline", action="append", default=[], metavar="JSON")
    return parser


def _load_report(
    *,
    v1_paths: list[str],
    v2_paths: list[str],
    now_ms: int | None,
) -> dict[str, Any]:
    dataset, effective_now = load_dataset_for_clock(
        v1_paths=v1_paths,
        v2_paths=v2_paths,
        now_ms=now_ms,
    )
    return {
        "ok": True,
        "operation": "load",
        "input_hash": dataset_input_hash(dataset),
        "default_now_ms": dataset.default_now_ms(),
        "now_ms": effective_now,
        "sources": [
            source.to_dict()
            for source in sorted(dataset.sources, key=lambda item: item.source_id)
        ],
        "candidates": [
            candidate.to_report_dict()
            for candidate in sorted(
                dataset.candidates,
                key=lambda item: (item.task_key, item.artifact, item.source_id),
            )
        ],
        "tombstones": [
            tombstone.to_dict()
            for tombstone in sorted(
                dataset.tombstones,
                key=lambda item: (item.task_key, item.artifact, item.digest, item.source_id),
            )
        ],
        "summary": {
            "source_count": len(dataset.sources),
            "candidate_count": len(dataset.candidates),
            "tombstone_count": len(dataset.tombstones),
        },
    }


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "load":
        return _load_report(
            v1_paths=args.v1,
            v2_paths=args.v2,
            now_ms=args.now_ms,
        )
    if args.command == "merge":
        dataset, effective_now = load_dataset_for_clock(
            v1_paths=args.v1,
            v2_paths=args.v2,
            now_ms=args.now_ms,
        )
        store = StateStore(args.state_dir, lock_timeout_seconds=args.lock_timeout_seconds)
        return execute_merge(
            store,
            v1_paths=args.v1,
            v2_paths=args.v2,
            now_ms=effective_now,
            clock_tolerance_ms=args.clock_tolerance_ms,
            fault_point=args.fault_point,
            dataset=dataset,
        )
    if args.command == "judge":
        pipelines = load_pipelines(args.pipeline)
        store = StateStore(args.state_dir, lock_timeout_seconds=args.lock_timeout_seconds)
        return execute_judge(
            store,
            pipelines=pipelines,
            now_ms=args.now_ms,
            clock_tolerance_ms=args.clock_tolerance_ms,
        )
    if args.command == "replace":
        descriptor, incoming = parse_package(args.package, now_ms=args.now_ms)
        pipelines = load_pipelines(args.pipeline)
        store = StateStore(args.state_dir, lock_timeout_seconds=args.lock_timeout_seconds)
        effective_now = args.now_ms
        if effective_now is None:
            effective_now = max(
                store.logical_clock_watermark(),
                int(descriptor["logical_clock_ms"]),
            )
            if effective_now != int(descriptor["logical_clock_ms"]):
                descriptor, incoming = parse_package(
                    args.package,
                    now_ms=effective_now,
                )
        return execute_replace(
            store,
            package_path=str(args.package),
            package_descriptor=descriptor,
            incoming=incoming,
            pipelines=pipelines,
            now_ms=effective_now,
            clock_tolerance_ms=args.clock_tolerance_ms,
            fault_point=args.fault_point,
        )
    if args.command == "resume":
        store = StateStore(args.state_dir, lock_timeout_seconds=args.lock_timeout_seconds)
        return execute_resume(
            store,
            clock_tolerance_ms=args.clock_tolerance_ms,
        )
    if args.command == "status":
        store = StateStore(args.state_dir, lock_timeout_seconds=args.lock_timeout_seconds)
        status = store.status()
        return {
            "ok": True,
            "operation": "status",
            **status,
        }
    if args.command == "verify":
        if args.v1 or args.v2:
            load_dataset(v1_paths=args.v1, v2_paths=args.v2)
        if args.pipeline:
            load_pipelines(args.pipeline)
        store = StateStore(args.state_dir, lock_timeout_seconds=args.lock_timeout_seconds)
        return execute_verify(
            store,
            v1_paths=args.v1,
            v2_paths=args.v2,
            pipeline_paths=args.pipeline,
        )
    raise CacheError(
        CACHE_INTERNAL_ERROR,
        "unknown command",
        fields={"command": args.command},
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except CacheError as exc:
        sys.stdout.write(pretty_json(exc.to_dict()))
        return exc.exit_code
    try:
        report = dispatch(args)
        sys.stdout.write(pretty_json(report))
        if report.get("ok") is False:
            return 2
        return 0
    except CacheError as exc:
        sys.stdout.write(pretty_json(exc.to_dict()))
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        error = CacheError(
            CACHE_INTERNAL_ERROR,
            "unhandled internal error",
            fields={"type": type(exc).__name__, "reason": str(exc)},
            exit_code=3,
        )
        sys.stdout.write(pretty_json(error.to_dict()))
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
