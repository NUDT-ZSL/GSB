from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .engine import Engine
from .errors import EngineError


def _print(value: Any, *, pretty: bool = True) -> None:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    print(text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metric-engine",
        description="Deterministic partitioned metric engine",
    )
    parser.add_argument(
        "--workspace",
        default="./runtime",
        help="workspace for state/, data/, buffer/ and logs/ (default: ./runtime)",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact JSON",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    load = sub.add_parser("load", help="validate configuration and initialize state")
    load.add_argument("--streams", default="samples/streams.json")
    load.add_argument("--partitions", default="samples/partitions.json")
    load.add_argument("--quotas", default="samples/quotas.json")
    load.add_argument("--tokens", default="samples/tokens.json")

    ingest = sub.add_parser("ingest", help="ingest a local JSON event batch")
    ingest.add_argument("--events", default="samples/events.json")
    ingest.add_argument(
        "--no-auto-flush",
        action="store_true",
        help="do not trigger a flush round when a partition buffer is full",
    )

    flush = sub.add_parser("flush", help="run deterministic flush rounds")
    flush.add_argument("--clock-ms", type=int, required=True)
    flush.add_argument("--rounds", type=int, default=1)
    flush.add_argument("--trigger", default="manual")

    resume = sub.add_parser("resume", help="repair data tails and redo missing ledger samples")
    resume.add_argument("--clock-ms", type=int)

    sub.add_parser("status", help="emit stable sorted JSON status")
    sub.add_parser("verify", help="verify ledger, partitions, buffers and data files")
    return parser


def _normalize_global_args(argv: list[str]) -> list[str]:
    globals_: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--compact":
            globals_.append(token)
            index += 1
        elif token == "--workspace" and index + 1 < len(argv):
            globals_.extend([token, argv[index + 1]])
            index += 2
        elif token.startswith("--workspace="):
            globals_.append(token)
            index += 1
        else:
            rest.append(token)
            index += 1
    return globals_ + rest


def main(argv: list[str] | None = None) -> int:
    normalized = _normalize_global_args(list(sys.argv[1:] if argv is None else argv))
    args = _parser().parse_args(normalized)
    pretty = not args.compact
    workspace = Path(args.workspace)
    try:
        if args.command == "load":
            _engine, report = Engine.load(
                workspace,
                streams_path=args.streams,
                partitions_path=args.partitions,
                quotas_path=args.quotas,
                tokens_path=args.tokens,
            )
            _print(report, pretty=pretty)
            return 0
        engine = Engine.open(workspace)
        if args.command == "ingest":
            report = engine.ingest_file(
                args.events, auto_flush=not args.no_auto_flush
            )
        elif args.command == "flush":
            report = engine.flush(
                clock_ms=args.clock_ms,
                rounds=args.rounds,
                trigger=args.trigger,
            )
        elif args.command == "resume":
            report = engine.resume(clock_ms=args.clock_ms)
        elif args.command == "status":
            report = engine.status()
        elif args.command == "verify":
            report = engine.verify()
        else:
            raise AssertionError(args.command)
        _print(report, pretty=pretty)
        return 0
    except EngineError as exc:
        if exc.code == "E_VERIFY_FAILED":
            payload = exc.details.get("verify", exc.as_dict())
            _print(payload, pretty=pretty)
            return 1
        _print(exc.as_dict(), pretty=pretty)
        return 2


if __name__ == "__main__":
    sys.exit(main())

