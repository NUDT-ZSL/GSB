from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .engine import ColdStartEngine
from .errors import BusinessError
from .loader import load_inputs


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coldstart-ledger")
    parser.add_argument("--input-dir", default=".", help="directory containing the four JSON inputs")
    parser.add_argument("--db", default="coldstart-ledger.db", help="SQLite ledger path")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run-round", help="schedule one deterministic cold-start round")
    run.add_argument("--round-id", required=True)
    run.add_argument("--now-ms", type=int)

    preempt = sub.add_parser("plan-preemption", help="simulate a preemption chain")
    preempt.add_argument("--instance-id", required=True)
    preempt.add_argument("--now-ms", type=int)

    sub.add_parser("audit", help="audit quota conservation")
    sub.add_parser("recover", help="reopen and verify the current ledger")
    sub.add_parser("snapshot", help="print the full current ledger snapshot")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        bundle = load_inputs(Path(args.input_dir))
        engine = ColdStartEngine(args.db, bundle)
        if args.command == "run-round":
            result = engine.run_round(args.round_id, args.now_ms)
            _print(result.to_dict())
        elif args.command == "plan-preemption":
            _print(engine.plan_preemption(args.instance_id, args.now_ms))
        elif args.command == "audit":
            _print(engine.audit())
        elif args.command == "recover":
            _print(engine.recover())
        elif args.command == "snapshot":
            _print(engine.snapshot())
        else:
            parser.error(f"unknown command {args.command}")
            return 2
        return 0
    except BusinessError as exc:
        _print({"error": exc.to_dict()})
        return 2
    except (OSError, ValueError) as exc:
        _print({"error": {"code": "CS_IO_ERROR", "message": str(exc)}})
        return 1


if __name__ == "__main__":
    sys.exit(main())

