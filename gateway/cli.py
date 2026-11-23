from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .engine import GatewayEngine
from .errors import E_BAD_SAMPLE, GatewayError
from .validation import load_json_file


DEFAULT_DB = os.environ.get("GATEWAY_DB", "data/gateway.db")


def _emit(payload: Any) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def _global_db_default() -> str:
    return os.environ.get("GATEWAY_DB", DEFAULT_DB)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gateway",
        description="Deterministic ordered-delivery gateway CLI",
    )
    parser.add_argument(
        "--db",
        default=_global_db_default(),
        help="SQLite database path (default: env GATEWAY_DB or data/gateway.db)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_db(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--db",
            default=argparse.SUPPRESS,
            help="SQLite database path",
        )

    load = subparsers.add_parser("load", help="validate sample and initialize state")
    add_db(load)
    load.add_argument("--topics", required=True)
    load.add_argument("--events")
    load.add_argument("--subscriptions", required=True)
    load.add_argument("--config", required=True)
    load.add_argument(
        "--no-events",
        action="store_true",
        help="initialize metadata without ingesting the events file",
    )

    produce = subparsers.add_parser("produce", help="append producer events")
    add_db(produce)
    produce.add_argument("--events", required=True)

    deliver = subparsers.add_parser("deliver", help="run a catch-up delivery round")
    add_db(deliver)
    deliver.add_argument("--subscriber", required=True)
    deliver.add_argument("--round-id")
    deliver.add_argument("--budget-ms", type=int)

    resume = subparsers.add_parser(
        "resume",
        help="recover interrupted in-flight state and optionally continue",
    )
    add_db(resume)
    resume.add_argument("--subscriber")
    resume.add_argument("--round-id")
    resume.add_argument("--budget-ms", type=int)

    status = subparsers.add_parser("status", help="emit stable partition status")
    add_db(status)

    verify = subparsers.add_parser(
        "verify",
        help="verify offsets, delivery idempotency, and ledger consistency",
    )
    add_db(verify)

    confirm = subparsers.add_parser(
        "confirm",
        help="idempotently confirm an in-flight delivery",
    )
    add_db(confirm)
    confirm.add_argument("--delivery-id", required=True)
    confirm.add_argument("--subscriber", required=True)
    return parser


def _load_documents(args: argparse.Namespace) -> tuple[Any, Any, Any, Any]:
    topics = load_json_file(args.topics, "topics")
    subscriptions = load_json_file(args.subscriptions, "subscriptions")
    config = load_json_file(args.config, "config")
    if args.no_events or not args.events:
        events: Any = {"events": []}
    else:
        events = load_json_file(args.events, "events")
    return topics, events, subscriptions, config


def _run(args: argparse.Namespace) -> int:
    engine = GatewayEngine(args.db)

    if args.command == "load":
        topics, events, subscriptions, config = _load_documents(args)
        result = engine.load_documents(
            topics,
            events,
            subscriptions,
            config,
            ingest_events=not args.no_events and bool(args.events),
        )
        _emit(result)
        return 0

    if args.command == "produce":
        events = load_json_file(args.events, "events")
        _emit(engine.produce(events))
        return 0

    if args.command == "deliver":
        result = engine.deliver(
            args.subscriber,
            round_id=args.round_id,
            budget_override_ms=args.budget_ms,
        )
        _emit(result)
        return 0

    if args.command == "resume":
        result = engine.resume(
            subscriber_id=args.subscriber,
            round_id=args.round_id,
            budget_override_ms=args.budget_ms,
        )
        _emit(result)
        return 0

    if args.command == "status":
        _emit(engine.status())
        return 0

    if args.command == "verify":
        result = engine.verify()
        _emit(result)
        return 0 if result.get("ok") else 1

    if args.command == "confirm":
        result = engine.confirm(
            args.delivery_id,
            subscriber_id=args.subscriber,
        )
        _emit(result)
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except GatewayError as exc:
        _emit(exc.to_dict())
        return 2 if exc.code == E_BAD_SAMPLE else 3
    except KeyboardInterrupt:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "E_INTERRUPTED",
                    "message": "operation interrupted",
                    "details": {},
                },
            }
        )
        return 130
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "E_INTERNAL",
                    "message": str(exc),
                    "details": {"type": type(exc).__name__},
                },
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
