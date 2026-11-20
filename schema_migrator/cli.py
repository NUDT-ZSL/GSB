from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .errors import WorkbenchError, error
from .manager import (
    explain_workspace,
    load_workspace,
    resume_workspace,
    run_migration,
    run_migration_all,
    status_workspace,
    verify_workspace,
)
from .util import pretty_json


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m schema_migrator",
        description="Local SQLite schema migration and compatibility diagnostics workbench",
    )
    parser.add_argument(
        "--workspace",
        default=".schema-workbench",
        help="workspace directory for catalog and immutable snapshots (default: .schema-workbench)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_workspace_override(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--workspace",
            default=argparse.SUPPRESS,
            help=argparse.SUPPRESS,
        )

    load = subparsers.add_parser("load", help="validate samples and materialize the first schema version")
    add_workspace_override(load)
    load.add_argument("--schemas", default="samples/schemas.json")
    load.add_argument("--migrations", default="samples/migrations.json")
    load.add_argument("--queries", default="samples/queries.json")
    load.add_argument("--tolerance", type=float, default=None)

    migrate = subparsers.add_parser("migrate", help="apply an incremental migration")
    add_workspace_override(migrate)
    selector = migrate.add_mutually_exclusive_group()
    selector.add_argument("--migration", help="migration id, for example m001")
    selector.add_argument("--all", action="store_true", help="apply all migrations up to the latest version")
    selector.add_argument("--to", type=int, help="apply migrations until the target version")
    migrate.add_argument(
        "--failpoint",
        choices=["add_column", "alter_column_type", "drop_column", "create_index", "drop_index", "replace_view", "replace_trigger"],
        help="test-only hard-exit after the named operation commits to the hidden stage",
    )

    explain = subparsers.add_parser("explain", help="explain one template's migration decision")
    add_workspace_override(explain)
    explain.add_argument("--template", required=True)
    explain.add_argument("--migration", default=None)

    resume = subparsers.add_parser("resume", help="recover an interrupted migration")
    add_workspace_override(resume)
    resume.add_argument(
        "--failpoint",
        choices=["add_column", "alter_column_type", "drop_column", "create_index", "drop_index", "replace_view", "replace_trigger"],
        help=argparse.SUPPRESS,
    )

    status = subparsers.add_parser("status", help="show the current committed schema version and object versions")
    add_workspace_override(status)
    verify = subparsers.add_parser("verify", help="compare catalog metadata with the actual SQLite schema")
    add_workspace_override(verify)
    return parser


def _print_json(value: Dict[str, Any]) -> None:
    sys.stdout.write(pretty_json(value))
    sys.stdout.write("\n")


def main(argv: Optional[list] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    workspace = Path(args.workspace)
    try:
        if args.command == "load":
            result = load_workspace(
                workspace,
                Path(args.schemas),
                Path(args.migrations),
                Path(args.queries),
                tolerance=args.tolerance,
            )
        elif args.command == "migrate":
            if args.all or args.to is not None:
                result = run_migration_all(
                    workspace,
                    target_version=args.to,
                    failpoint=args.failpoint,
                )
            elif args.migration:
                result = run_migration(workspace, args.migration, failpoint=args.failpoint)
            else:
                raise error(
                    "E_USAGE",
                    "migrate requires --migration, --all, or --to",
                )
        elif args.command == "explain":
            result = explain_workspace(
                workspace,
                args.template,
                migration_id=args.migration,
            )
        elif args.command == "resume":
            result = resume_workspace(workspace, failpoint=args.failpoint)
        elif args.command == "status":
            result = status_workspace(workspace)
        elif args.command == "verify":
            result = verify_workspace(workspace)
        else:
            raise error("E_USAGE", "unknown command", command=args.command)
    except WorkbenchError as exc:
        _print_json(exc.to_dict())
        return exc.exit_code
    except KeyboardInterrupt:
        _print_json(
            {
                "ok": False,
                "error": {
                    "code": "E_INTERRUPTED",
                    "message": "operation interrupted before commit",
                    "details": {},
                },
            }
        )
        return 130
    except Exception as exc:
        _print_json(
            {
                "ok": False,
                "error": {
                    "code": "E_INTERNAL",
                    "message": "unexpected local execution failure",
                    "details": {
                        "type": type(exc).__name__,
                        "detail": str(exc),
                    },
                },
            }
        )
        return 3
    _print_json(result)
    if args.command == "verify" and not result.get("ok", False):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

