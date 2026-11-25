"""Command-line interface for the deterministic batch ledger."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any
from typing import TextIO

from . import __version__
from .db import LedgerDB
from .engine import BatchLedgerEngine
from .errors import BusinessError
from .fixture import INJECTION_PHASES, load_fixture


def _default_database() -> str:
    return "batchledger.db"


def _emit(
    value: Any, *, pretty: bool = False, stream: TextIO | None = None
) -> None:
    target = stream
    if pretty:
        print(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            ),
            file=target,
        )
    else:
        print(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=target,
        )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=_default_database(),
        help=f"SQLite state database (default: {_default_database()})",
    )
    parser.add_argument(
        "--pretty", action="store_true", help="pretty-print stable sorted JSON"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="batchledger",
        description=(
            "Deterministic fixed-point batch scheduler, SQLite ledger and "
            "interrupted-run verifier."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser(
        "load", help="validate and load a deterministic fixture"
    )
    load_parser.add_argument("fixture", nargs="?", help="fixture directory or JSON bundle")
    load_parser.add_argument(
        "--fixture", dest="fixture_option", help="fixture directory or JSON bundle"
    )
    _add_common(load_parser)
    load_parser.add_argument(
        "--reset",
        action="store_true",
        help="replace an existing state database after successful validation",
    )

    run_parser = subparsers.add_parser("run", help="advance closed logical windows")
    _add_common(run_parser)
    run_parser.add_argument(
        "--clock-ms",
        type=int,
        default=None,
        help="injected logical clock; only windows with end_ms <= clock are closed",
    )
    run_parser.add_argument(
        "--no-injections",
        action="store_true",
        help="ignore pending SIGKILL checkpoints (used for deterministic baselines)",
    )

    resume_parser = subparsers.add_parser(
        "resume", help="resume an interrupted state from persisted SQLite state"
    )
    _add_common(resume_parser)
    resume_parser.add_argument(
        "--clock-ms",
        type=int,
        default=None,
        help="optional injected logical clock for the resumed run",
    )
    resume_parser.add_argument(
        "--no-injections",
        action="store_true",
        help="ignore pending SIGKILL checkpoints",
    )

    inject_parser = subparsers.add_parser(
        "inject", help="register a deterministic SIGKILL checkpoint"
    )
    _add_common(inject_parser)
    inject_parser.add_argument("--window-index", type=int, required=True)
    inject_parser.add_argument(
        "--phase", required=True, choices=sorted(INJECTION_PHASES)
    )
    inject_parser.add_argument("--replica-id", default=None)
    inject_parser.add_argument("--batch-index", type=int, default=None)
    inject_parser.add_argument("--injection-id", default=None)
    inject_parser.add_argument(
        "--signal", default="SIGKILL", choices=["SIGKILL"]
    )

    status_parser = subparsers.add_parser(
        "status", help="emit stable sorted JSON for windows and requests"
    )
    _add_common(status_parser)

    verify_parser = subparsers.add_parser(
        "verify",
        help="replay a one-shot baseline and compare every semantic field",
    )
    _add_common(verify_parser)

    generate_parser = subparsers.add_parser(
        "generate", help="generate the fixed-seed local sample"
    )
    generate_parser.add_argument("--output", default="samples/default")
    generate_parser.add_argument("--seed", type=int, default=20261126)
    generate_parser.add_argument(
        "--pretty", action="store_true", help="pretty-print stable sorted JSON"
    )
    return parser


def _validate_injection_target(
    db: LedgerDB,
    *,
    replica_id: str | None,
    window_index: int,
    phase: str,
    batch_index: int | None,
) -> None:
    assert db.conn is not None
    if window_index < 0:
        raise BusinessError(
            "E_INJECTION_WINDOW_INVALID", "window index must be non-negative"
        )
    if batch_index is not None and batch_index < 0:
        raise BusinessError(
            "E_INJECTION_BATCH_INVALID", "batch index must be non-negative"
        )
    fixture = db.fixture()
    if replica_id is not None and replica_id not in fixture.replica_map():
        raise BusinessError(
            "E_INJECTION_REPLICA_INVALID", f"unknown replica: {replica_id}"
        )
    query = (
        "SELECT COUNT(*) AS n FROM windows WHERE window_index = ? "
        + ("AND replica_id = ?" if replica_id is not None else "")
    )
    params: tuple[Any, ...] = (
        (window_index, replica_id) if replica_id is not None else (window_index,)
    )
    count = int(db.conn.execute(query, params).fetchone()["n"])
    if count == 0:
        raise BusinessError(
            "E_INJECTION_TARGET_NOT_FOUND",
            f"no routed window matches index={window_index}, replica={replica_id or '*'}",
        )
    if phase in ("batch_adjudication", "batch_commit", "batch_execute"):
        # A null batch index means "any batch" and is deliberately supported.
        pass


def _command_load(args: argparse.Namespace) -> int:
    fixture_path = args.fixture_option or args.fixture
    if not fixture_path:
        raise BusinessError(
            "E_FIXTURE_ARGUMENT_REQUIRED",
            "provide a fixture directory/bundle positionally or with --fixture",
        )
    fixture = load_fixture(fixture_path)
    result = LedgerDB.initialize(args.db, fixture, replace=args.reset)
    result.update(
        {
            "ok": True,
            "command": "load",
            "fixture_source": fixture.source,
            "model_id": fixture.model.model_id,
            "counts": {
                "replicas": len(fixture.replicas),
                "requests": len(fixture.requests),
                "injections": len(fixture.injections),
            },
        }
    )
    _emit(result, pretty=args.pretty)
    return 0


def _command_run(args: argparse.Namespace, *, command: str) -> int:
    engine = BatchLedgerEngine(args.db)
    result = engine.run(
        clock_ms=args.clock_ms,
        honor_injections=not args.no_injections,
    )
    result["command"] = command
    _emit(result, pretty=args.pretty)
    return 0


def _command_inject(args: argparse.Namespace) -> int:
    if not Path(args.db).expanduser().exists():
        raise BusinessError(
            "E_STATE_NOT_FOUND", f"state database does not exist: {args.db}"
        )
    with LedgerDB(args.db).lock():
        with LedgerDB(args.db) as db:
            _validate_injection_target(
                db,
                replica_id=args.replica_id,
                window_index=args.window_index,
                phase=args.phase,
                batch_index=args.batch_index,
            )
            injection_id = args.injection_id or (
                f"inject-w{args.window_index:08d}-{args.phase}-"
                f"b{args.batch_index if args.batch_index is not None else 'any'}-"
                f"{args.replica_id or 'any'}"
            )
            result = db.add_injection(
                injection_id=injection_id,
                replica_id=args.replica_id,
                window_index=args.window_index,
                phase=args.phase,
                batch_index=args.batch_index,
                signal_name=args.signal,
                enabled=True,
            )
    _emit(
        {"ok": True, "command": "inject", "database": args.db, **result},
        pretty=args.pretty,
    )
    return 0


def _command_status(args: argparse.Namespace) -> int:
    result = BatchLedgerEngine(args.db).status()
    _emit(result, pretty=args.pretty)
    return 0


def _command_verify(args: argparse.Namespace) -> int:
    result = BatchLedgerEngine(args.db).verify()
    _emit(result, pretty=args.pretty)
    return 0 if result["ok"] else 4


def _command_generate(args: argparse.Namespace) -> int:
    from tools.generate_samples import generate

    result = generate(Path(args.output), args.seed)
    _emit(result, pretty=args.pretty)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "load":
            return _command_load(args)
        if args.command == "run":
            return _command_run(args, command="run")
        if args.command == "resume":
            return _command_run(args, command="resume")
        if args.command == "inject":
            return _command_inject(args)
        if args.command == "status":
            return _command_status(args)
        if args.command == "verify":
            return _command_verify(args)
        if args.command == "generate":
            return _command_generate(args)
        parser.error(f"unknown command: {args.command}")
    except BusinessError as exc:
        _emit(exc.to_dict(), pretty=args.pretty, stream=sys.stderr)
        return exc.exit_code
    except sqlite3.Error as exc:  # pragma: no cover - defensive CLI guard
        error = BusinessError("E_STORAGE", f"SQLite operation failed: {exc}")
        _emit(error.to_dict(), pretty=args.pretty, stream=sys.stderr)
        return error.exit_code
    except OSError as exc:  # pragma: no cover - defensive CLI guard
        error = BusinessError("E_IO", f"filesystem operation failed: {exc}")
        _emit(error.to_dict(), pretty=args.pretty, stream=sys.stderr)
        return error.exit_code
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
