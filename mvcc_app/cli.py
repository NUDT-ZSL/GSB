"""Command-line interface for the deterministic MVCC editor."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

from .engine import Engine
from .errors import BusinessError, ErrorCode
from .store import Store
from .util import canonical_json, require_positive_int
from .validation import (
    load_config,
    load_sessions,
    load_table,
)

DEFAULT_DB = "data/mvcc.db"


def _json_value(raw: str | None, field: str) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            ErrorCode.INVALID_OPERATION,
            f"{field} must be valid JSON",
            {"field": field, "line": exc.lineno, "column": exc.colno},
        ) from exc


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=os.environ.get("MVCC_DB"),
        help="SQLite database path (default: data/mvcc.db or MVCC_DB)",
    )
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mvcc-edit",
        description="Deterministic MVCC visibility editor",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="validate samples and initialize the database")
    _add_common(init)
    init.add_argument("--table", required=True, help="table description JSON")
    init.add_argument("--config", required=True, help="engine configuration JSON")
    init.add_argument("--sessions", required=True, help="session script JSON")
    init.add_argument("--replay", action="store_true", help="replay all session events after init")

    edit = subparsers.add_parser("edit", help="replay sessions or apply one interactive edit")
    _add_common(edit)
    edit.add_argument("--table", help="table JSON, used only for first-run initialization")
    edit.add_argument("--config", help="config JSON, used only for first-run initialization")
    edit.add_argument("--sessions", help="session script JSON to replay")
    edit.add_argument("--txn-id", type=int, help="transaction id for an interactive edit")
    edit.add_argument(
        "--type",
        choices=["insert", "update", "delete"],
        help="interactive operation type",
    )
    edit.add_argument("--clock-ms", type=int, help="injected logical clock in milliseconds")
    edit.add_argument("--seq", type=int, help="optional operation sequence number")
    edit.add_argument("--row", help="insert row JSON")
    edit.add_argument("--key", help="primary-key JSON")
    edit.add_argument("--changes", help="update changes JSON")
    edit.add_argument(
        "--finish",
        action="store_true",
        help="finish the transaction instead of applying an operation",
    )
    edit.add_argument(
        "--outcome",
        choices=["committed", "rolled_back"],
        default="committed",
        help="finish outcome",
    )
    edit.add_argument(
        "--crash-after-stage",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    edit.add_argument("--crash-at-event", type=int, help=argparse.SUPPRESS)

    read = subparsers.add_parser("read", help="run a deterministic range read")
    _add_common(read)
    read.add_argument("--txn-id", type=int, required=True)
    read.add_argument("--clock-ms", type=int, help="injected query clock")
    read.add_argument("--view-id", help="query an existing retained read view")
    read.add_argument("--range", help="full range specification JSON")
    read.add_argument("--where", help="range where-clause JSON")
    read.add_argument("--order-by", help="comma-separated order columns")
    read.add_argument("--limit", type=int)

    gc = subparsers.add_parser("gc", help="collect eligible versions")
    _add_common(gc)
    gc.add_argument("--clock-ms", type=int, help="injected GC clock")
    gc.add_argument("--watermark-txn", type=int, help="GC transaction-number watermark")
    gc.add_argument("--async", dest="async_gc", action="store_true", help="start a detached worker")
    gc.add_argument(
        "--crash-after-stage",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    resume = subparsers.add_parser("resume", help="clean orphan generations and replay missing steps")
    _add_common(resume)
    resume.add_argument("--sessions", help="optional session script to check/replay")
    resume.add_argument(
        "--crash-after-stage",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    resume.add_argument("--crash-at-event", type=int, help=argparse.SUPPRESS)

    status = subparsers.add_parser("status", help="show version, view, and GC status")
    _add_common(status)

    verify = subparsers.add_parser("verify", help="verify integrity and deterministic replay")
    _add_common(verify)
    verify.add_argument("--runs", type=int, default=2)
    verify.add_argument("--gc-clock-ms", type=int)

    worker = subparsers.add_parser("_gc_worker", help="internal async GC worker")
    worker.add_argument("--db", required=True)
    worker.add_argument("--clock-ms", type=int)
    worker.add_argument("--watermark-txn", type=int)
    return parser


def _resolve_db(args: argparse.Namespace, config: dict[str, Any] | None = None) -> str:
    if getattr(args, "db", None):
        return str(args.db)
    if config is not None:
        return str(config["db_path"])
    return DEFAULT_DB


def _require_database(db_path: str) -> None:
    if not Store.exists(db_path):
        raise BusinessError(
            ErrorCode.DATABASE_NOT_INITIALIZED,
            "database does not exist; run init first",
            {"db_path": db_path},
        )


def _open_engine(db_path: str) -> tuple[Engine, Store]:
    _require_database(db_path)
    store = Store(db_path)
    return Engine(store), store


def _initialize(
    args: argparse.Namespace,
    *,
    replay: bool,
) -> dict[str, Any]:
    config = load_config(args.config)
    table = load_table(args.table)
    sessions = load_sessions(args.sessions, config["clock_step_ms"])
    db_path = _resolve_db(args, config)
    store = Store(db_path)
    init_result = store.initialize(table, config, sessions)
    result: dict[str, Any] = {
        "ok": True,
        "command": "init",
        **init_result,
        "transactions_registered": len(sessions["sessions"]),
    }
    if replay:
        replay_result = Engine(store).replay_sessions(sessions)
        result["replay"] = {
            "events": replay_result["events"],
            "transactions": replay_result["transactions"],
            "last_clock_ms": replay_result["last_clock_ms"],
        }
    return result


def _next_sequence(store: Store, txn_id: int) -> int:
    _, state = store.load_head()
    sequences = [
        applied["seq"]
        for applied in state["applied_ops"].values()
        if applied["txn_id"] == txn_id and applied["phase"] == "operation"
    ]
    return max(sequences, default=0) + 1


def _handle_init(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    db_path = _resolve_db(args, config)
    if Store.exists(db_path):
        raise BusinessError(
            ErrorCode.DATABASE_EXISTS,
            "database already exists",
            {"db_path": db_path},
        )
    return _initialize(args, replay=args.replay)


def _handle_edit(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "crash_at_event", None) is not None:
        require_positive_int(args.crash_at_event, "crash_at_event")
    if args.sessions:
        if args.finish or args.txn_id is not None or args.type is not None:
            raise BusinessError(
                ErrorCode.INVALID_OPERATION,
                "session replay cannot be combined with interactive edit flags",
                {},
            )
        if not Store.exists(_resolve_db(args)):
            if not (args.table and args.config):
                raise BusinessError(
                    ErrorCode.DATABASE_NOT_INITIALIZED,
                    "first-run edit requires --table and --config",
                    {"db_path": _resolve_db(args)},
                )
            init = _initialize(args, replay=False)
            return {
                "ok": True,
                "command": "edit",
                "initialized": True,
                "init": init,
                "result": _replay_from_files(args),
            }
        return _replay_from_files(args)

    db_path = _resolve_db(args)
    engine, store = _open_engine(db_path)
    if args.txn_id is None:
        raise BusinessError(
            ErrorCode.INVALID_OPERATION,
            "interactive edit requires --txn-id",
            {},
        )
    if args.finish:
        event_result = engine.commit_transaction(
            args.txn_id,
            clock_ms=args.clock_ms,
            outcome=args.outcome,
            crash_after_stage=args.crash_after_stage,
        )
        return {"ok": True, "command": "edit", "result": event_result}
    if args.type is None:
        raise BusinessError(
            ErrorCode.INVALID_OPERATION,
            "interactive edit requires --type or --finish",
            {},
        )
    sequence = args.seq if args.seq is not None else _next_sequence(store, args.txn_id)
    operation: dict[str, Any] = {
        "type": args.type,
        "seq": sequence,
    }
    if args.clock_ms is not None:
        operation["clock_ms"] = args.clock_ms
    if args.type == "insert":
        operation["row"] = _json_value(args.row, "row")
    elif args.type == "update":
        operation["key"] = _json_value(args.key, "key")
        operation["changes"] = _json_value(args.changes, "changes")
    else:
        operation["key"] = _json_value(args.key, "key")
    event_result = engine.edit_operation(
        args.txn_id,
        operation,
        crash_after_stage=args.crash_after_stage,
    )
    return {"ok": True, "command": "edit", "result": event_result}


def _replay_from_files(args: argparse.Namespace) -> dict[str, Any]:
    engine, store = _open_engine(_resolve_db(args))
    _, state = store.load_head()
    config = state["config"]
    sessions = load_sessions(args.sessions, config["clock_step_ms"])
    return engine.replay_sessions(
        sessions,
        crash_after_stage=args.crash_after_stage,
        crash_event_index=getattr(args, "crash_at_event", None),
    )


def _handle_read(args: argparse.Namespace) -> dict[str, Any]:
    engine, _ = _open_engine(_resolve_db(args))
    if args.range:
        range_raw = _json_value(args.range, "range")
    else:
        range_raw = {}
        if args.where:
            range_raw["where"] = _json_value(args.where, "where")
        if args.order_by:
            range_raw["order_by"] = [
                item.strip() for item in args.order_by.split(",") if item.strip()
            ]
        if args.limit is not None:
            range_raw["limit"] = args.limit
    return engine.read(
        args.txn_id,
        clock_ms=args.clock_ms,
        range_raw=range_raw,
        view_id=args.view_id,
    )


def _handle_gc(args: argparse.Namespace) -> dict[str, Any]:
    db_path = _resolve_db(args)
    _require_database(db_path)
    if args.async_gc:
        command = [
            sys.executable,
            "-m",
            "mvcc_app",
            "_gc_worker",
            "--db",
            db_path,
        ]
        if args.clock_ms is not None:
            command.extend(["--clock-ms", str(args.clock_ms)])
        if args.watermark_txn is not None:
            command.extend(["--watermark-txn", str(args.watermark_txn)])
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        return {
            "ok": True,
            "command": "gc",
            "status": "started",
            "job_id": "gc:async",
            "pid": process.pid,
            "clock_ms": args.clock_ms,
            "watermark_txn": args.watermark_txn,
        }
    engine, _ = _open_engine(db_path)
    return engine.run_gc(
        clock_ms=args.clock_ms,
        watermark_txn=args.watermark_txn,
        crash_after_stage=args.crash_after_stage,
    )


def _handle_resume(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "crash_at_event", None) is not None:
        require_positive_int(args.crash_at_event, "crash_at_event")
    engine, store = _open_engine(_resolve_db(args))
    sessions = None
    if args.sessions:
        _, state = store.load_head()
        sessions = load_sessions(
            args.sessions,
            state["config"]["clock_step_ms"],
        )
    return engine.resume(
        sessions,
        crash_after_stage=args.crash_after_stage,
        crash_event_index=getattr(args, "crash_at_event", None),
    )


def _handle_status(args: argparse.Namespace) -> dict[str, Any]:
    engine, _ = _open_engine(_resolve_db(args))
    return engine.status()


def _handle_verify(args: argparse.Namespace) -> dict[str, Any]:
    engine, _ = _open_engine(_resolve_db(args))
    return engine.verify_repeatability(
        runs=args.runs,
        gc_clock_ms=args.gc_clock_ms,
    )


def _handle_gc_worker(args: argparse.Namespace) -> dict[str, Any]:
    engine, _ = _open_engine(args.db)
    return engine.run_gc(
        clock_ms=args.clock_ms,
        watermark_txn=args.watermark_txn,
    )


def _emit(value: dict[str, Any], *, pretty: bool) -> None:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    else:
        text = canonical_json(value)
    print(text)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "init": _handle_init,
        "edit": _handle_edit,
        "read": _handle_read,
        "gc": _handle_gc,
        "resume": _handle_resume,
        "status": _handle_status,
        "verify": _handle_verify,
        "_gc_worker": _handle_gc_worker,
    }
    try:
        result = handlers[args.command](args)
        _emit(result, pretty=bool(getattr(args, "pretty", False)))
        return 0
    except BusinessError as exc:
        _emit(exc.as_dict(), pretty=bool(getattr(args, "pretty", False)))
        return exc.exit_code
    except Exception as exc:
        if os.environ.get("MVCC_DEBUG"):
            raise
        error = BusinessError(
            ErrorCode.INTERNAL_ERROR,
            "unexpected internal error",
            {"exception": type(exc).__name__, "message": str(exc)},
        )
        _emit(error.as_dict(), pretty=bool(getattr(args, "pretty", False)))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

