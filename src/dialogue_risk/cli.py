"""Command line interface for local deterministic operation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import AppError
from .service import DomainService


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _service(args: argparse.Namespace, *, reset: bool = False) -> DomainService:
    root = Path(args.root).resolve()
    data_dir = Path(args.data_dir)
    runtime_dir = Path(args.runtime_dir)
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    if not runtime_dir.is_absolute():
        runtime_dir = root / runtime_dir
    return DomainService(data_dir, runtime_dir, reset=reset)


def _add_global_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="workspace root")
    parser.add_argument("--data-dir", default="data", help="sample input directory")
    parser.add_argument(
        "--runtime-dir", default="runtime", help="snapshot and runtime directory"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dialogue-risk",
        description="Deterministic customer-service sentiment and risk editor",
    )
    _add_global_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="initialize or load snapshot")
    init_parser.add_argument(
        "--reset",
        action="store_true",
        help="discard runtime snapshot and rebuild from committed samples",
    )

    batch_parser = subparsers.add_parser("batch", help="run full batch judgment")
    batch_parser.add_argument(
        "--snapshot-only",
        action="store_true",
        help="return stored snapshot results without fresh recomputation",
    )
    batch_parser.add_argument("--output", type=Path, help="also write JSON to this file")

    query_parser = subparsers.add_parser("query", help="query one conversation")
    query_parser.add_argument("conv_id")
    query_parser.add_argument("--budget-ms", type=float)
    query_parser.add_argument(
        "--inject-delay-ms",
        type=float,
        default=0.0,
        help="actual sleep injection used only to demonstrate latency failure",
    )

    text_parser = subparsers.add_parser("edit-text", help="edit one message text")
    text_parser.add_argument("conv_id")
    text_parser.add_argument("msg_no", type=int)
    text_parser.add_argument("text")

    weight_parser = subparsers.add_parser("edit-weight", help="edit one message weight")
    weight_parser.add_argument("conv_id")
    weight_parser.add_argument("msg_no", type=int)
    weight_parser.add_argument("weight", type=float)

    split_parser = subparsers.add_parser("split", help="split a conversation slice")
    split_parser.add_argument("conv_id")
    split_parser.add_argument("at_msg_no", type=int)
    split_parser.add_argument("new_conv_id")

    merge_parser = subparsers.add_parser("merge", help="merge two conversation slices")
    merge_parser.add_argument("left_conv_id")
    merge_parser.add_argument("right_conv_id")
    merge_parser.add_argument("--new-conv-id")

    subparsers.add_parser("self-check", help="run cross-module consistency checks")
    subparsers.add_parser("snapshot-info", help="show versions and checksums")

    verify_parser = subparsers.add_parser(
        "snapshot-verify", help="verify an external snapshot file"
    )
    verify_parser.add_argument("file", type=Path)
    verify_parser.add_argument(
        "--allow-rollback",
        action="store_true",
        help="verify checksums without rejecting older versions",
    )

    restore_parser = subparsers.add_parser(
        "snapshot-restore", help="whole-replace runtime with a newer snapshot"
    )
    restore_parser.add_argument("file", type=Path)

    subparsers.add_parser("cache-info", help="show cache keys and statistics")

    demo_parser = subparsers.add_parser(
        "demo", help="run baseline, query, edit invalidation and anomaly demo"
    )
    demo_parser.add_argument(
        "--report",
        type=Path,
        help="write the complete demo report to this JSON file",
    )
    return parser


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "init":
        service = _service(args, reset=args.reset)
        return {"ok": True, **service.snapshot_info()}

    if args.command == "demo":
        from .demo import run_demo

        root = Path(args.root).resolve()
        data_dir = Path(args.data_dir)
        if not data_dir.is_absolute():
            data_dir = root / data_dir
        report = run_demo(data_dir)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
        return report

    service = _service(args)
    if args.command == "batch":
        result = service.run_batch(fresh_recompute=not args.snapshot_only)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
        return result
    if args.command == "query":
        return service.query(
            args.conv_id,
            budget_ms=args.budget_ms,
            inject_delay_ms=args.inject_delay_ms,
        )
    if args.command == "edit-text":
        return {"ok": True, "commit": service.edit_text(args.conv_id, args.msg_no, args.text)}
    if args.command == "edit-weight":
        return {
            "ok": True,
            "commit": service.edit_weight(args.conv_id, args.msg_no, args.weight),
        }
    if args.command == "split":
        return {
            "ok": True,
            "commit": service.split_conversation(
                args.conv_id, args.at_msg_no, args.new_conv_id
            ),
        }
    if args.command == "merge":
        return {
            "ok": True,
            "commit": service.merge_conversations(
                args.left_conv_id, args.right_conv_id, args.new_conv_id
            ),
        }
    if args.command == "self-check":
        return service.self_check()
    if args.command == "snapshot-info":
        return {"ok": True, **service.snapshot_info()}
    if args.command == "snapshot-verify":
        return {
            "ok": True,
            **service.verify_snapshot_file(
                args.file, enforce_no_rollback=not args.allow_rollback
            ),
        }
    if args.command == "snapshot-restore":
        return {"ok": True, **service.restore_snapshot(args.file)}
    if args.command == "cache-info":
        return {"ok": True, **service.cache_snapshot()}
    raise AssertionError(f"unhandled command {args.command}")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = dispatch(args)
    except AppError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True, indent=2))
        return 2
    except (OSError, ValueError) as exc:
        error = AppError("E_INTERNAL", str(exc))
        print(json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True, indent=2))
        return 2
    _json_print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

