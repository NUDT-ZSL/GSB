"""Command line interface: run / resume / status / verify."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import PipelineError
from .pipeline import load_json_file, load_pipeline
from .scheduler import Scheduler
from .store import Ledger
from .verify import verify as verify_run


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False))


def _load_inputs(args) -> list[dict]:
    raw = load_json_file(args.input, "input records")
    records = raw.get("records") if isinstance(raw, dict) else None
    if not isinstance(records, list):
        raise PipelineError("E_BAD_PIPELINE", "input file must contain a 'records' list", path=args.input)
    return records


def _cmd_run(args, resume: bool) -> int:
    pipeline = load_pipeline(args.pipeline)
    records = _load_inputs(args)
    db_path = Path(args.db)
    out_path = Path(args.out)
    if not resume:
        for path in (db_path, out_path):
            if path.exists():
                path.unlink()
        for suffix in ("-wal", "-shm"):
            side = Path(str(db_path) + suffix)
            if side.exists():
                side.unlink()
    scheduler = Scheduler(pipeline, records, db_path, out_path, crash_after_node=args.crash_after_node)
    try:
        summary = scheduler.run()
    finally:
        scheduler.close()
    _emit({"command": "resume" if resume else "run", "pipeline": pipeline.name, "summary": summary})
    return 0


def _cmd_status(args) -> int:
    ledger = Ledger(args.db)
    try:
        report = {
            "clock": ledger.get_clock(),
            "nodes": ledger.all_nodes(),
            "watermarks": ledger.watermarks(),
        }
    finally:
        ledger.close()
    _emit(report)
    return 0


def _cmd_verify(args) -> int:
    pipeline = load_pipeline(args.pipeline)
    records = _load_inputs(args)
    report = verify_run(pipeline, records, args.db, args.out)
    _emit(report)
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pipeline_tool", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p, need_io=True):
        p.add_argument("--pipeline", required=True, help="pipeline description JSON")
        p.add_argument("--input", required=True, help="input records JSON")
        p.add_argument("--db", required=True, help="SQLite ledger path")
        if need_io:
            p.add_argument("--out", required=True, help="result file path")

    p_run = sub.add_parser("run", help="start a fresh run (removes previous state)")
    add_common(p_run)
    p_run.add_argument("--crash-after-node", default=None,
                       help="test hook: hard-exit after committing this node")
    p_resume = sub.add_parser("resume", help="resume from persisted state")
    add_common(p_resume)
    p_resume.add_argument("--crash-after-node", default=None, help=argparse.SUPPRESS)
    p_status = sub.add_parser("status", help="dump node states as stable-sorted JSON")
    p_status.add_argument("--db", required=True)
    p_verify = sub.add_parser("verify", help="verify ledger/results/trace consistency")
    add_common(p_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _cmd_run(args, resume=False)
        if args.command == "resume":
            return _cmd_run(args, resume=True)
        if args.command == "status":
            return _cmd_status(args)
        if args.command == "verify":
            return _cmd_verify(args)
    except PipelineError as exc:
        _emit({"error": exc.to_dict()})
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
