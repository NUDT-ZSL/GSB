from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from . import engine
from .errors import EngineError


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="field-engine",
        description="Deterministic local columnar aggregation and sliding-window engine",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "resume", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--experiment", required=True, help="path to experiment.json")
        command.add_argument("--db", required=True, help="path to the SQLite state database")
    status = sub.add_parser("status")
    status.add_argument("--db", required=True, help="path to the SQLite state database")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            result = engine.run(args.experiment, args.db)
            _print(result)
            return 0 if result.get("ok") else 4
        if args.command == "resume":
            result = engine.resume(args.experiment, args.db)
            _print(result)
            return 0 if result.get("ok") else 4
        if args.command == "status":
            _print(engine.status(args.db))
            return 0
        if args.command == "verify":
            result = engine.verify(args.experiment, args.db)
            _print(result)
            return 0 if result.get("ok") else 5
    except EngineError as exc:
        _print({"ok": False, "error": exc.to_dict()})
        return exc.exit_code
    except Exception as exc:
        _print(
            {
                "ok": False,
                "error": {
                    "code": "E_INTERNAL",
                    "message": f"{type(exc).__name__}: {exc}",
                    "details": {},
                },
            }
        )
        return 70
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
