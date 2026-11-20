from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from .errors import GSBError, GSBUsageError
from .runner import CrashInjector, Engine, reset_state, standalone_status
from .util import stable_json
from .validation import load_sample


def _common_sample_options(parser: argparse.ArgumentParser, *, reset: bool = False) -> None:
    parser.add_argument("--sample", type=Path, required=True, help="sample directory")
    parser.add_argument("--db", type=Path, default=Path("state/gsb.sqlite"))
    parser.add_argument("--work", type=Path, default=Path("work"))
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument("--query", action="append", default=[], help="query id to process")
    parser.add_argument("--worker-id", default=f"pid-{os.getpid()}")
    parser.add_argument("--crash-at", default=None, help="deterministic failure injection")
    if reset:
        parser.add_argument(
            "--no-reset",
            action="store_true",
            help="continue an existing state database instead of clearing it",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gsb",
        description="Deterministic local grouped-aggregation diagnostics workbench",
    )
    parser.add_argument("--version", action="version", version="gsb 0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="start a fresh run")
    _common_sample_options(run_parser, reset=True)
    run_parser.add_argument(
        "--batch-index",
        type=int,
        action="append",
        default=[],
        help="stage only selected batch indexes (concurrency testing)",
    )

    resume_parser = subparsers.add_parser("resume", help="resume persisted work")
    _common_sample_options(resume_parser)
    resume_parser.add_argument(
        "--batch-index",
        type=int,
        action="append",
        default=[],
        help="stage only selected batch indexes (concurrency testing)",
    )

    status_parser = subparsers.add_parser("status", help="print stable state diagnostics")
    status_parser.add_argument("--db", type=Path, default=Path("state/gsb.sqlite"))
    status_parser.add_argument("--work", type=Path, default=Path("work"))
    status_parser.add_argument("--query", action="append", default=[])

    verify_parser = subparsers.add_parser("verify", help="verify persisted state and output")
    verify_parser.add_argument("--sample", type=Path, required=True)
    verify_parser.add_argument("--db", type=Path, default=Path("state/gsb.sqlite"))
    verify_parser.add_argument("--work", type=Path, default=Path("work"))
    verify_parser.add_argument("--out", type=Path, default=Path("out"))
    verify_parser.add_argument("--query", action="append", default=[])
    return parser


def _select_queries(sample: Any, requested: list[str]) -> list[Any]:
    if not requested:
        return list(sample.queries)
    wanted = set(requested)
    available = {query.query_id for query in sample.queries}
    missing = sorted(wanted - available)
    if missing:
        raise GSBUsageError(
            "requested query id does not exist in sample",
            {"missing": missing, "available": sorted(available)},
        )
    return [query for query in sample.queries if query.query_id in wanted]


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(stable_json(payload))
    sys.stdout.flush()


def cmd_run(args: argparse.Namespace, *, resume: bool) -> int:
    sample = load_sample(args.sample)
    queries = _select_queries(sample, args.query)
    if not resume and not args.no_reset:
        reset_state(args.db, args.work, args.out)
    crash = CrashInjector(args.crash_at)
    selected_batches = set(args.batch_index) if args.batch_index else None
    with Engine(sample, args.db, args.work, args.out, crash) as engine:
        reports: list[dict[str, Any]] = []
        try:
            for query in queries:
                reports.append(
                    engine.process_query(
                        query,
                        args.worker_id,
                        only_batch_indexes=selected_batches,
                    )
                )
        except GSBError:
            for query in queries:
                record = engine.storage.get_query(query.query_id)
                if record is not None and record["status"] == "running":
                    engine.storage.mark_failed(query.query_id, "execution failed")
            raise
        payload = {
            "schema_version": 1,
            "ok": all(report.get("status") == "complete" for report in reports),
            "database": str(args.db.resolve()),
            "queries": sorted(reports, key=lambda item: item["query_id"]),
        }
        _emit(payload)
        return 0 if payload["ok"] else 0


def cmd_status(args: argparse.Namespace) -> int:
    payload = standalone_status(args.db, args.work)
    if args.query:
        wanted = set(args.query)
        payload["queries"] = [
            item for item in payload["queries"] if item["query_id"] in wanted
        ]
    _emit(payload)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    sample = load_sample(args.sample)
    queries = _select_queries(sample, args.query)
    with Engine(sample, args.db, args.work, args.out) as engine:
        reports = [engine.verify_query(query) for query in queries]
    payload = {
        "schema_version": 1,
        "ok": all(report["ok"] for report in reports),
        "database": str(args.db.resolve()),
        "queries": sorted(reports, key=lambda item: item["query_id"]),
    }
    _emit(payload)
    return 0 if payload["ok"] else 7


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return cmd_run(args, resume=False)
        if args.command == "resume":
            return cmd_run(args, resume=True)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "verify":
            return cmd_verify(args)
        raise GSBUsageError("unknown command", {"command": args.command})
    except GSBError as exc:
        sys.stderr.write(stable_json(exc.to_dict()))
        return exc.exit_code
    except Exception as exc:
        payload = {
            "ok": False,
            "error": {
                "code": "E_INTERNAL",
                "message": str(exc),
                "type": type(exc).__name__,
            },
        }
        if os.environ.get("GSB_DEBUG"):
            payload["error"]["traceback"] = traceback.format_exc()
        sys.stderr.write(stable_json(payload))
        return 9


if __name__ == "__main__":
    raise SystemExit(main())

