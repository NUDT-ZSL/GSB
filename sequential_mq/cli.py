"""Stable JSON command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Any, Sequence

from .engine import Engine
from .errors import E_DATABASE, E_INTERNAL, E_INPUT_JSON, EngineError
from .model import read_json, validate_sample


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _emit(value: Any) -> None:
    sys.stdout.write(stable_json(value) + "\n")
    sys.stdout.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smq",
        description="Local deterministic sequential message engine",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("SMQ_DB"),
        help="SQLite database path (or SMQ_DB)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="validate and idempotently ingest a sample")
    load.add_argument("--sample", required=True, help="sample JSON path")
    load.add_argument(
        "--injections",
        help="override the injection manifest path from sample config",
    )

    produce = subparsers.add_parser("produce", help="produce one batch")
    source = produce.add_mutually_exclusive_group(required=True)
    source.add_argument("--batch-file", help="JSON file containing one production batch")
    source.add_argument("--batch-json", help="inline production batch JSON")
    source.add_argument("--sample", help="sample file from which to select a batch")
    produce.add_argument("--batch-seq", type=int, help="batch_seq when using --sample")

    commit = subparsers.add_parser("commit", help="commit a prepared transaction")
    commit.add_argument("--tx-id", required=True)
    commit.add_argument("--logical-ms", type=int, required=True)
    commit.add_argument("--expected-digest")

    rollback = subparsers.add_parser("rollback", help="roll back a prepared transaction")
    rollback.add_argument("--tx-id", required=True)
    rollback.add_argument("--logical-ms", type=int, required=True)

    consume = subparsers.add_parser("consume", help="pull visible messages from one partition")
    consume.add_argument("--consumer-id", required=True)
    consume.add_argument("--topic", required=True)
    consume.add_argument("--partition", type=int, required=True)
    consume.add_argument("--max", type=int, default=10, dest="max_messages")
    consume.add_argument("--logical-ms", type=int, required=True)

    ack = subparsers.add_parser("ack", help="idempotently acknowledge a delivered offset")
    ack.add_argument("--consumer-id", required=True)
    ack.add_argument("--topic", required=True)
    ack.add_argument("--partition", type=int, required=True)
    ack.add_argument("--offset", type=int, required=True)
    ack.add_argument("--logical-ms", type=int, required=True)

    resume = subparsers.add_parser("resume", help="replay unfinished steps and expire transactions")
    resume.add_argument("--logical-ms", type=int)

    subparsers.add_parser("status", help="emit stable partition state")
    subparsers.add_parser("verify", help="verify persisted state and digest chains")
    return parser


def _load_batch_argument(args: argparse.Namespace) -> Any:
    if args.batch_json is not None:
        try:
            return json.loads(args.batch_json)
        except json.JSONDecodeError as exc:
            raise EngineError(
                E_INPUT_JSON,
                "cannot decode inline batch JSON",
                details={"reason": str(exc)},
            ) from exc
    if args.batch_file is not None:
        return read_json(args.batch_file, "batch")
    if args.sample is None:
        raise EngineError(E_INPUT_JSON, "no batch source was provided")
    if args.batch_seq is None:
        raise EngineError(
            E_INPUT_JSON,
            "--batch-seq is required with --sample",
        )
    sample = validate_sample(args.sample)
    for raw_batch in read_json(args.sample, "sample")["batches"]:
        if raw_batch.get("batch_seq") == args.batch_seq:
            return raw_batch
    raise EngineError(
        E_INPUT_JSON,
        "batch_seq is not present in the sample",
        details={"batch_seq": args.batch_seq, "sample": str(args.sample)},
    )


def _run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if not args.db:
        raise EngineError(
            E_DATABASE,
            "a database path is required via --db or SMQ_DB",
            exit_code=3,
        )
    engine = Engine(args.db)
    if args.command == "load":
        return (
            engine.load_sample(args.sample, injection_override=args.injections),
            0,
        )
    if args.command == "produce":
        return engine.produce(_load_batch_argument(args)), 0
    if args.command == "commit":
        return (
            engine.commit(
                args.tx_id,
                args.logical_ms,
                expected_digest=args.expected_digest,
            ),
            0,
        )
    if args.command == "rollback":
        return engine.rollback(args.tx_id, args.logical_ms), 0
    if args.command == "consume":
        return (
            engine.consume(
                args.consumer_id,
                args.topic,
                args.partition,
                args.max_messages,
                args.logical_ms,
            ),
            0,
        )
    if args.command == "ack":
        return (
            engine.ack(
                args.consumer_id,
                args.topic,
                args.partition,
                args.offset,
                args.logical_ms,
            ),
            0,
        )
    if args.command == "resume":
        return engine.resume(args.logical_ms), 0
    if args.command == "status":
        return engine.status(), 0
    if args.command == "verify":
        result = engine.verify()
        return result, 0 if result["ok"] else 1
    raise EngineError(E_INTERNAL, "unsupported command", exit_code=3)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result, exit_code = _run(args)
        _emit(result)
        return exit_code
    except EngineError as exc:
        _emit({"command": args.command, "error": exc.as_dict()})
        return exc.exit_code
    except sqlite3.Error as exc:
        error = EngineError(
            E_DATABASE,
            "SQLite operation failed",
            details={"reason": str(exc)},
            exit_code=3,
        )
        _emit({"command": args.command, "error": error.as_dict()})
        return error.exit_code
    except (OSError, UnicodeError) as exc:
        error = EngineError(
            E_INTERNAL,
            "local I/O operation failed",
            details={"reason": str(exc)},
            exit_code=3,
        )
        _emit({"command": args.command, "error": error.as_dict()})
        return error.exit_code
    except Exception as exc:
        if os.environ.get("SMQ_DEBUG") == "1":
            raise
        error = EngineError(
            E_INTERNAL,
            "unexpected engine failure",
            details={"exception": type(exc).__name__, "reason": str(exc)},
            exit_code=3,
        )
        _emit({"command": args.command, "error": error.as_dict()})
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

