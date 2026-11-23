from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .engine import AppService
from .errors import AppError, ErrorCode
from .util import stable_json

DEFAULT_DB = "walcp_state.db"
DEFAULT_NOW_MS = 1_700_000_500_000
DEFAULT_ANCHOR_MS = 1_700_000_100_000


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite state database")
    parser.add_argument(
        "--now-ms",
        type=int,
        default=DEFAULT_NOW_MS,
        help="injected logical clock in Unix epoch milliseconds",
    )
    parser.add_argument(
        "--clock-tolerance-ms",
        type=int,
        default=0,
        help="allowed backwards clock tolerance in milliseconds",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="walcp",
        description=(
            "Deterministic WAL replay, v1/v2 checkpoint interoperability, "
            "edit invalidation, and crash recovery."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    load = sub.add_parser("load", help="validate samples and initialize state")
    _common(load)
    load.add_argument("--samples", default="samples")
    load.add_argument(
        "--anchor-ms",
        type=int,
        default=None,
        help="clock anchor used to convert v1 relative saved seconds",
    )

    apply = sub.add_parser("apply", help="apply missing WAL transactions")
    _common(apply)
    apply.add_argument(
        "--clock-step-ms",
        type=int,
        default=0,
        help="deterministic logical clock increment per replayed transaction",
    )

    edit = sub.add_parser("edit", help="edit or delete one WAL row")
    _common(edit)
    edit.add_argument("--txn-id", type=int, required=True)
    edit.add_argument("--delete", action="store_true")
    edit.add_argument(
        "--set",
        dest="changes",
        action="append",
        default=[],
        metavar="FIELD=JSON",
        help="set table, key, op, before, after, checkpoint_gen, or commit_ms",
    )

    checkpoint = sub.add_parser(
        "checkpoint", help="create, receive, or list checkpoint packages"
    )
    _common(checkpoint)
    checkpoint.add_argument(
        "action",
        nargs="?",
        choices=("create", "receive", "list"),
        default="create",
    )
    checkpoint.add_argument("--out", help="output directory for a new v2 package")
    checkpoint.add_argument("--package", help="incoming v2 package directory")
    checkpoint.add_argument(
        "--full",
        action="store_true",
        help="build a complete-replacement package",
    )

    resume = sub.add_parser("resume", help="recover interrupted work")
    _common(resume)
    resume.add_argument("--clock-step-ms", type=int, default=0)

    status = sub.add_parser("status", help="emit stable current state JSON")
    _common(status)

    verify = sub.add_parser(
        "verify", help="compare incremental replay with full WAL replay"
    )
    _common(verify)
    return parser


def _parse_changes(assignments: list[str]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    allowed = {
        "table",
        "key",
        "op",
        "before",
        "after",
        "checkpoint_gen",
        "commit_ms",
    }
    for assignment in assignments:
        if "=" not in assignment:
            raise AppError(
                ErrorCode.UNSUPPORTED_EDIT,
                "--set requires FIELD=JSON",
                {"assignment": assignment},
            )
        field, raw_value = assignment.split("=", 1)
        if field not in allowed:
            raise AppError(
                ErrorCode.UNSUPPORTED_EDIT,
                "unsupported edit field",
                {"field": field, "allowed": sorted(allowed)},
            )
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            if field in {"table", "key", "op"} and raw_value:
                value = raw_value
            else:
                raise AppError(
                    ErrorCode.UNSUPPORTED_EDIT,
                    "edit value is not valid JSON",
                    {"field": field, "value": raw_value, "reason": str(exc)},
                ) from exc
        changes[field] = value
    return changes


def _load_anchor(samples: Path, explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    manifest_path = samples / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            anchor = manifest.get("v1_anchor_ms")
            if isinstance(anchor, int):
                return anchor
        except (OSError, json.JSONDecodeError):
            pass
    return DEFAULT_ANCHOR_MS


def _dispatch(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    service = AppService(Path(args.db))
    if args.command == "load":
        samples = Path(args.samples)
        result = service.load(
            samples,
            now_ms=args.now_ms,
            anchor_ms=_load_anchor(samples, args.anchor_ms),
            tolerance_ms=args.clock_tolerance_ms,
        )
        return result, 0
    if args.command == "apply":
        result = service.apply(
            now_ms=args.now_ms,
            clock_step_ms=args.clock_step_ms,
            tolerance_ms=args.clock_tolerance_ms,
        )
        return result, 0
    if args.command == "edit":
        changes = _parse_changes(args.changes)
        result = service.edit(
            args.txn_id,
            changes=changes,
            delete=args.delete,
            now_ms=args.now_ms,
            tolerance_ms=args.clock_tolerance_ms,
        )
        return result, 0
    if args.command == "checkpoint":
        if args.action == "list":
            return service.checkpoint_list(), 0
        if args.action == "receive":
            if not args.package:
                raise AppError(
                    ErrorCode.CHECKPOINT_INVALID,
                    "checkpoint receive requires --package",
                )
            result = service.checkpoint(
                package=Path(args.package),
                now_ms=args.now_ms,
                tolerance_ms=args.clock_tolerance_ms,
            )
            return result, 0
        if not args.out:
            raise AppError(
                ErrorCode.CHECKPOINT_INVALID,
                "checkpoint create requires --out",
            )
        result = service.checkpoint(
            out_dir=Path(args.out),
            full_replace=args.full,
            now_ms=args.now_ms,
            tolerance_ms=args.clock_tolerance_ms,
        )
        return result, 0
    if args.command == "resume":
        result = service.resume(
            now_ms=args.now_ms,
            clock_step_ms=args.clock_step_ms,
            tolerance_ms=args.clock_tolerance_ms,
        )
        return result, 0
    if args.command == "status":
        return service.status(), 0
    if args.command == "verify":
        result = service.verify()
        return result, 0 if result["ok"] else 3
    raise AppError(ErrorCode.STATE_NOT_INITIALIZED, "unknown command")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result, exit_code = _dispatch(args)
        print(stable_json(result))
        return exit_code
    except AppError as exc:
        print(stable_json(exc.as_dict()))
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(
            stable_json(
                {
                    "ok": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": str(exc),
                        "details": {},
                    },
                }
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
