from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import Engine
from .errors import EngineError


def _json_print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="columnar-engine",
        description="Local deterministic columnar pushdown and recovery engine",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="initialize a local SQLite engine database")
    init.add_argument("--db", default="data/engine.db")
    init.add_argument("--work-dir", default="var")

    run = sub.add_parser("run", help="rewrite and execute a sample")
    run.add_argument("--sample", default="samples/default")
    run.add_argument("--batches")
    run.add_argument("--plan")
    run.add_argument("--config")
    run.add_argument("--db", default="data/engine.db")
    run.add_argument("--work-dir", default="var")
    run.add_argument("--fail-shard-id", type=int)
    run.add_argument("--crash-after-results", type=int)
    run.add_argument("--crash-after-commits", type=int)
    run.add_argument("--logical-clock-ms", type=float)

    resume = sub.add_parser("resume", help="resume or repeat a run idempotently")
    resume.add_argument("--sample", default="samples/default")
    resume.add_argument("--batches")
    resume.add_argument("--plan")
    resume.add_argument("--config")
    resume.add_argument("--db", default="data/engine.db")
    resume.add_argument("--work-dir", default="var")

    verify = sub.add_parser("verify-rewrite", help="verify rewrite equivalence only")
    verify.add_argument("--sample", default="samples/default")
    verify.add_argument("--batches")
    verify.add_argument("--plan")
    verify.add_argument("--config")

    inspect = sub.add_parser("inspect", help="read a committed execution snapshot")
    inspect.add_argument("--run-id", required=True)
    inspect.add_argument("--db", default="data/engine.db")
    inspect.add_argument("--work-dir", default="var")
    return parser


def _paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    sample = Path(args.sample)
    return (
        Path(args.batches) if args.batches else sample / "batches.json",
        Path(args.plan) if args.plan else sample / "plan.json",
        Path(args.config) if args.config else sample / "config.json",
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            Engine.initialize(args.db, args.work_dir)
            _json_print({"ok": True, "db_path": str(args.db), "work_dir": str(args.work_dir)})
            return 0
        if args.command == "verify-rewrite":
            batches, plan, config = _paths(args)
            engine = Engine(args.db if hasattr(args, "db") else "data/engine.db")
            _json_print({"ok": True, "rewrite": engine.verify_rewrite(batches, plan, config)})
            return 0
        if args.command == "inspect":
            engine = Engine(args.db, args.work_dir)
            _json_print({"ok": True, "snapshot": engine.snapshot(args.run_id)})
            return 0
        batches, plan, config = _paths(args)
        engine = Engine(args.db, args.work_dir)
        summary = engine.run_paths(
            batches,
            plan,
            config,
            fail_shard_id=getattr(args, "fail_shard_id", None),
            crash_after_results=getattr(args, "crash_after_results", None),
            crash_after_commits=getattr(args, "crash_after_commits", None),
            logical_clock_ms=getattr(args, "logical_clock_ms", None),
        )
        _json_print({"ok": True, "summary": summary.to_dict()})
        return 0
    except EngineError as error:
        _json_print({"ok": False, "error": error.to_dict()})
        return 2
    except OSError as error:
        _json_print({"ok": False, "error": {"code": "E_IO", "message": str(error)}})
        return 3


if __name__ == "__main__":
    sys.exit(main())

