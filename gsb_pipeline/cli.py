"""Command-line interface for deterministic plan and state verification."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from .errors import (
    BusinessError,
    E_INPUT_JSON,
    E_PATCH_INVALID,
    E_VIEW_MISSING,
)
from .models import load_and_merge_sample, load_view_definitions
from .store import (
    apply_patch_stream,
    initialize_state_with_definitions,
    resume_state,
    status_state,
    verify_state,
)
from .utils import pretty_json


def _state_db(args: argparse.Namespace) -> str:
    if getattr(args, "state_db", None):
        return args.state_db
    return str(Path(args.sample_dir) / "pipeline_state.db")


def _merge_summary(merged: dict[str, Any]) -> dict[str, Any]:
    return {
        "format": merged["format"],
        "snapshot_generation": merged["snapshot_generation"],
        "logical_clock_ms": merged["logical_clock_ms"],
        "source_fingerprint": merged["source_fingerprint"],
        "counts": merged["counts"],
        "order": merged["order"],
        "views": [
            {
                "name": view["name"],
                "source_version": view["source_version"],
                "source_plan_generation": view["source_plan_generation"],
                "absolute_time_ms": view["absolute_time_ms"],
                "decision": view["decision"],
                "columns": view["columns"],
                "plan_summary": view["plan_summary"],
            }
            for view in merged["views"]
        ],
        "tombstones": merged["tombstones"],
    }


def _read_events_file(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BusinessError(
            E_INPUT_JSON,
            "补丁事件文件不可读",
            path=str(path),
            error=str(exc),
        ) from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BusinessError(
                E_INPUT_JSON,
                "补丁事件 JSONL 行解析失败",
                path=str(path),
                line=line_number,
                error=str(exc),
            ) from exc
        if not isinstance(value, dict):
            raise BusinessError(
                E_PATCH_INVALID,
                "补丁事件必须是 JSON 对象",
                path=str(path),
                line=line_number,
            )
        events.append(value)
    return events


def command_load(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    definitions, v1, v2, merged = load_and_merge_sample(
        args.sample_dir,
        base_clock_ms=args.base_clock_ms,
    )
    state = initialize_state_with_definitions(
        _state_db(args),
        definitions,
        merged,
        force=args.force,
    )
    return (
        {
            "ok": True,
            "command": "load",
            "sample_dir": str(Path(args.sample_dir)),
            "state_db": _state_db(args),
            "snapshots": {
                "v1_views": len(v1.views),
                "v1_logical_clock_ms": v1.logical_clock_ms,
                "v2_views": len(v2.views),
                "v2_tombstones": len(v2.tombstones),
                "v2_plan_generation": v2.snapshot_generation,
            },
            "merge": _merge_summary(merged),
            "state": state,
        },
        0,
    )


def command_parse(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    definitions = load_view_definitions(Path(args.sample_dir) / "view_definitions.json")
    if args.view:
        if args.view not in definitions.views:
            raise BusinessError(
                E_VIEW_MISSING,
                f"视图不存在: {args.view}",
                view=args.view,
            )
        names = [args.view]
    else:
        names = definitions.order
    return (
        {
            "ok": True,
            "command": "parse",
            "sample_dir": str(Path(args.sample_dir)),
            "format": "gsb-logical-plan-v1",
            "view_count": len(names),
            "order": definitions.order,
            "plans": [
                {
                    "name": name,
                    "dependencies": definitions.views[name].dependencies,
                    "plan_summary": definitions.views[name].plan["plan_summary"],
                    "plan": definitions.views[name].plan,
                }
                for name in sorted(names)
            ],
        },
        0,
    )


def command_merge(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    _definitions, _v1, _v2, merged = load_and_merge_sample(
        args.sample_dir,
        base_clock_ms=args.base_clock_ms,
    )
    return (
        {
            "ok": True,
            "command": "merge",
            "sample_dir": str(Path(args.sample_dir)),
            "merge": merged,
        },
        0,
    )


def command_patch(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    events_path = Path(args.events_file or (Path(args.sample_dir) / "patch_events.jsonl"))
    events = _read_events_file(events_path)
    if args.from_sequence is not None:
        events = [
            event
            for event in events
            if isinstance(event.get("sequence"), int)
            and event["sequence"] >= args.from_sequence
        ]
    if args.event_id:
        events = [event for event in events if event.get("patch_id") == args.event_id]
        if not events:
            raise BusinessError(
                E_PATCH_INVALID,
                f"事件文件中没有 patch_id: {args.event_id}",
            )
    result = apply_patch_stream(
        _state_db(args),
        events,
        clock_tolerance_ms=args.clock_tolerance_ms,
    )
    return (
        {
            **result,
            "command": "patch",
            "events_file": str(events_path),
        },
        0,
    )


def command_resume(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    result = resume_state(
        args.state_db,
        clock_tolerance_ms=args.clock_tolerance_ms,
    )
    return ({**result, "command": "resume"}, 0)


def command_status(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    return ({**status_state(args.state_db), "command": "status"}, 0)


def command_verify(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    result = verify_state(args.state_db)
    return ({**result, "command": "verify"}, 0 if result["ok"] else 2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gsb-pipeline",
        description="SQL logical-plan snapshot, merge, patch invalidation, and recovery tool",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="validate sample and initialize SQLite state")
    load.add_argument("--sample-dir", default="sample")
    load.add_argument("--state-db")
    load.add_argument("--base-clock-ms", type=int)
    load.add_argument("--force", action="store_true")
    load.set_defaults(handler=command_load)

    parse = subparsers.add_parser("parse", help="parse all view definitions into logical plans")
    parse.add_argument("--sample-dir", default="sample")
    parse.add_argument("--view")
    parse.set_defaults(handler=command_parse)

    merge = subparsers.add_parser("merge", help="merge v1/v2 snapshots and print unified plans")
    merge.add_argument("--sample-dir", default="sample")
    merge.add_argument("--base-clock-ms", type=int)
    merge.set_defaults(handler=command_merge)

    patch = subparsers.add_parser("patch", help="apply a patch JSONL stream")
    patch.add_argument("--state-db", required=True)
    patch.add_argument("--sample-dir", default="sample")
    patch.add_argument("--events-file")
    patch.add_argument("--event-id")
    patch.add_argument("--from-sequence", type=int)
    patch.add_argument("--clock-tolerance-ms", type=int)
    patch.set_defaults(handler=command_patch)

    resume = subparsers.add_parser("resume", help="finish persisted PREPARED/STAGED runs")
    resume.add_argument("--state-db", required=True)
    resume.add_argument("--clock-tolerance-ms", type=int)
    resume.set_defaults(handler=command_resume)

    status = subparsers.add_parser("status", help="show committed state and pending runs")
    status.add_argument("--state-db", required=True)
    status.set_defaults(handler=command_status)

    verify = subparsers.add_parser("verify", help="verify snapshot atomicity and plan integrity")
    verify.add_argument("--state-db", required=True)
    verify.set_defaults(handler=command_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result, exit_code = args.handler(args)
    except BusinessError as exc:
        sys.stdout.write(
            pretty_json(
                {
                    "ok": False,
                    "command": args.command,
                    "error": exc.to_dict(),
                }
            )
        )
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        sys.stdout.write(
            pretty_json(
                {
                    "ok": False,
                    "command": args.command,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                }
            )
        )
        return 3
    sys.stdout.write(pretty_json(result))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

