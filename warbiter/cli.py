from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .canonical import load_json_file, pretty_json
from .engine import (
    apply_script,
    list_conflicts,
    load_database_context,
    resolve_pending,
    resume,
    status_report,
    update_policy,
    verify,
)
from .errors import EXIT_CODES, WarbiterError
from .model import parse_config, parse_schema, parse_script
from .storage import initialize_database


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default="warbiter.db", help="SQLite database path")


def _add_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--schema", help="tenant/table JSON description")
    parser.add_argument("--config", help="arbiter configuration JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="warbiter",
        description="Deterministic write-write conflict arbitration ledger",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="initialize database or declare policy")
    _add_common(init_parser)
    _add_inputs(init_parser)

    apply_parser = sub.add_parser("apply", help="validate, stage, and commit a script")
    _add_common(apply_parser)
    _add_inputs(apply_parser)
    apply_parser.add_argument("--script", required=True)
    apply_parser.add_argument(
        "--resolve",
        action="store_true",
        help="resolve pending conflicts immediately after commit",
    )
    apply_parser.add_argument(
        "--fault-after",
        type=int,
        default=None,
        help="test hook: exit 137 after N staged transactions",
    )

    conflict_parser = sub.add_parser("conflict", help="detect and list conflicts")
    _add_common(conflict_parser)

    resolve_parser = sub.add_parser("resolve", help="resolve all pending conflicts")
    _add_common(resolve_parser)

    resume_parser = sub.add_parser("resume", help="recover and resolve unfinished work")
    _add_common(resume_parser)

    status_parser = sub.add_parser("status", help="stable status JSON")
    _add_common(status_parser)

    verify_parser = sub.add_parser("verify", help="verify ledger and full replay")
    _add_common(verify_parser)

    policy_parser = sub.add_parser(
        "policy", help="declare a new policy version without rewriting data"
    )
    _add_common(policy_parser)
    _add_inputs(policy_parser)

    return parser


def _read_schema_config(args: argparse.Namespace):
    if not args.schema and not args.config:
        return None, None
    if not args.schema or not args.config:
        raise WarbiterError(
            "E_BAD_ARGUMENT",
            "--schema and --config must be supplied together",
        )
    return (
        parse_schema(_load_json(args.schema, "E_BAD_SCHEMA", "schema")),
        parse_config(_load_json(args.config, "E_BAD_CONFIG", "config")),
    )


def _load_json(path: str, code: str, label: str):
    try:
        return load_json_file(path)
    except FileNotFoundError as error:
        raise WarbiterError(
            code,
            f"{label} file does not exist",
            {"path": path},
        ) from error
    except json.JSONDecodeError as error:
        raise WarbiterError(
            code,
            f"{label} file is not valid JSON",
            {"path": path, "line": error.lineno, "column": error.colno},
        ) from error
    except OSError as error:
        raise WarbiterError(
            code,
            f"{label} file cannot be read",
            {"path": path, "reason": str(error)},
        ) from error


def _schema_config_for_apply(args: argparse.Namespace):
    schema, config = _read_schema_config(args)
    if schema is not None and config is not None:
        return schema, config
    if not Path(args.db).exists():
        raise WarbiterError(
            "E_NOT_INITIALIZED",
            "database does not exist; supply --schema and --config",
        )
    return load_database_context(args.db)


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.write(pretty_json(value) + "\n")


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "init":
        schema, config = _read_schema_config(args)
        if schema is None or config is None:
            raise WarbiterError(
                "E_BAD_ARGUMENT",
                "init requires --schema and --config",
            )
        result = initialize_database(args.db, schema, config)
        return {
            "schema": "warbiter-init-v1",
            "db": args.db,
            **result,
        }
    if args.command == "policy":
        schema, config = _read_schema_config(args)
        if schema is None or config is None:
            raise WarbiterError(
                "E_BAD_ARGUMENT",
                "policy requires --schema and --config",
            )
        return update_policy(args.db, schema, config)
    if args.command == "apply":
        schema, config = _schema_config_for_apply(args)
        script = parse_script(
            _load_json(args.script, "E_BAD_SCRIPT", "script"), schema, config
        )
        if args.fault_after is not None:
            import os

            os.environ["WARBITER_FAULT_APPLY"] = str(args.fault_after)
        return apply_script(
            args.db,
            script,
            schema,
            config,
            do_resolve=args.resolve,
        )
    if args.command == "conflict":
        return list_conflicts(args.db)
    if args.command == "resolve":
        return resolve_pending(args.db)
    if args.command == "resume":
        return resume(args.db)
    if args.command == "status":
        return status_report(args.db)
    if args.command == "verify":
        return verify(args.db)
    raise WarbiterError("E_BAD_ARGUMENT", f"unknown command {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except WarbiterError as error:
        _emit(error.as_dict())
        return EXIT_CODES.get(error.code, 1)
    except sqlite3.Error as error:
        wrapped = WarbiterError("E_DB", str(error))
        _emit(wrapped.as_dict())
        return EXIT_CODES["E_DB"]
    _emit(result)
    return 0
if __name__ == "__main__":
    raise SystemExit(main())

