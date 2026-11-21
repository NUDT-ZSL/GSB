"""Command line interface for LockFair."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .engine import LockFairEngine
from .errors import BusinessError, ErrorCode, error
from .models import load_bundle


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUNDLE = PROJECT_ROOT / "samples" / "demo.json"
DEFAULT_DB = PROJECT_ROOT / "state" / "demo.sqlite"


def _json_print(payload: Any, *, compact: bool = False) -> None:
    if compact:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))


def _ensure_demo_database(db_path: Path, bundle_path: Path, force: bool = False) -> LockFairEngine:
    engine = LockFairEngine(db_path)
    try:
        engine.get_state()
    except BusinessError as exc:
        if exc.code != ErrorCode.STATE_NOT_INITIALIZED:
            raise
        engine.initialize(bundle_path, force=True)
    if force:
        engine.initialize(bundle_path, force=True)
    return engine


def _engine(args: argparse.Namespace) -> LockFairEngine:
    return LockFairEngine(Path(args.db))


def cmd_validate(args: argparse.Namespace) -> int:
    bundle = load_bundle(args.bundle)
    _json_print(
        {
            "ok": True,
            "source": bundle.source,
            "bundle_hash": bundle.canonical_hash(),
            "resources": len(bundle.resources),
            "holders": len(bundle.holders),
            "applications": len(bundle.applications),
            "config": bundle.config.to_dict(),
        },
        compact=args.compact,
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    bundle = load_bundle(args.bundle)
    engine = LockFairEngine(Path(args.db))
    result = engine.initialize(bundle, force=args.force)
    _json_print(
        {
            "ok": True,
            "db_path": result["db_path"],
            "snapshot_id": result["snapshot_id"],
            "state_version": result["state_version"],
            "state_hash": result["state_hash"],
            "bundle_hash": bundle.canonical_hash(),
        },
        compact=args.compact,
    )
    return 0


def _round_output(snapshot: dict[str, Any], compact: bool) -> None:
    summary = snapshot.get("summary") or snapshot.get("state", {}).get("last_round")
    payload = {
        "ok": True,
        "round_no": snapshot["round_no"],
        "logical_clock_ms": snapshot["logical_clock_ms"],
        "state_version": snapshot["version"],
        "state_hash": snapshot["state_hash"],
        "idempotent_replay": snapshot.get("idempotent_replay", False),
    }
    if summary:
        payload.update(
            {
                "used_budget_ms": summary.get("used_budget_ms"),
                "grants": summary.get("grants", []),
                "expired": summary.get("expired", []),
                "deferred_count": len(summary.get("deferred", [])),
                "deferred": summary.get("deferred", []),
                "forced_yields": summary.get("forced_yields", []),
                "fairness": summary.get("fairness", {}),
                "errors": summary.get("errors", []),
            }
        )
    _json_print(payload, compact=compact)


def cmd_step(args: argparse.Namespace) -> int:
    engine = _engine(args)
    if args.ensure_demo and not Path(args.db).exists():
        engine = _ensure_demo_database(Path(args.db), Path(args.bundle))
    snapshot = engine.execute_round(
        round_no=args.round,
        clock_ms=args.clock_ms,
        request_id=args.request_id,
        expected_version=args.expected_version,
    )
    _round_output(snapshot, args.compact)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    engine = _engine(args)
    if args.ensure_demo and not Path(args.db).exists():
        engine = _ensure_demo_database(Path(args.db), Path(args.bundle))
    results = engine.run_rounds(args.rounds)
    _json_print(
        {
            "ok": True,
            "rounds": [
                {
                    "round_no": item["round_no"],
                    "logical_clock_ms": item["logical_clock_ms"],
                    "state_version": item["version"],
                    "used_budget_ms": item.get("summary", {}).get("used_budget_ms"),
                    "granted": [grant["seq"] for grant in item.get("summary", {}).get("grants", [])],
                    "expired": [expired["seq"] for expired in item.get("summary", {}).get("expired", [])],
                    "errors": [issue["code"] for issue in item.get("summary", {}).get("errors", [])],
                }
                for item in results
            ],
            "state_version": results[-1]["version"] if results else None,
        },
        compact=args.compact,
    )
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    engine = _engine(args)
    snapshot = engine.get_state(round_no=args.round)
    if args.full:
        _json_print(snapshot, compact=args.compact)
    else:
        state = snapshot["state"]
        payload = {
            "ok": True,
            "round_no": snapshot["round_no"],
            "logical_clock_ms": snapshot["logical_clock_ms"],
            "state_version": snapshot["version"],
            "state_hash": snapshot["state_hash"],
            "kind": snapshot["kind"],
            "last_round": state.get("last_round"),
            "active": {
                key: value["active"]
                for key, value in state["resources"].items()
                if value.get("active")
            },
            "holders": {
                key: {
                    "occupied_count": value["occupied_count"],
                    "max_concurrent_grants": value["max_concurrent_grants"],
                }
                for key, value in state["holders"].items()
            },
            "queues": state["queues"],
        }
        _json_print(payload, compact=args.compact)
    return 0


def cmd_errors(args: argparse.Namespace) -> int:
    engine = _engine(args)
    _json_print({"ok": True, "errors": engine.list_errors(args.limit)}, compact=args.compact)
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    engine = _engine(args)
    events = engine.list_queue_events(round_no=args.round, limit=args.limit)
    _json_print({"ok": True, "events": events}, compact=args.compact)
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    engine = _engine(args)
    _json_print(engine.recover(), compact=args.compact)
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    engine = _engine(args)
    payload = {
        "seq": args.seq,
        "holder": args.holder,
        "lock_key": args.lock_key,
        "logical_clock_ms": args.arrival_ms,
        "desired_hold_ms": args.desired_hold_ms,
        "fairness_label": args.label,
        "priority": args.priority,
    }
    if args.expected_generation is not None:
        payload["expected_lease_generation"] = args.expected_generation
    result = engine.submit_application(payload, expected_version=args.expected_version)
    _json_print(result, compact=args.compact)
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    engine = _engine(args)
    result = engine.cancel_application(
        args.seq,
        mode="abandoned" if args.abandon else "cancelled",
        reason=args.reason,
    )
    _json_print(result, compact=args.compact)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import run_server

    db_path = Path(args.db)
    if not db_path.exists() or args.reset:
        LockFairEngine(db_path).initialize(Path(args.bundle), force=True)
    else:
        engine = _ensure_demo_database(db_path, Path(args.bundle))
    run_server(
        db_path=db_path,
        bundle_path=Path(args.bundle),
        host=args.host,
        port=args.port,
        open_browser=args.open,
    )
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    engine = LockFairEngine(Path(args.db))
    engine.initialize(Path(args.bundle), force=True)
    if args.headless:
        _json_print(
            {
                "ok": True,
                "db_path": args.db,
                "bundle": args.bundle,
                "message": "演示状态已重置",
            },
            compact=args.compact,
        )
        return 0
    from .web import run_server

    run_server(
        db_path=Path(args.db),
        bundle_path=Path(args.bundle),
        host=args.host,
        port=args.port,
        open_browser=args.open,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lockfair",
        description="本地分布式锁批量授予编排与公平性裁决组件",
    )
    parser.add_argument("--compact", action="store_true", help="输出单行 JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    output_parent = argparse.ArgumentParser(add_help=False)
    output_parent.add_argument(
        "--compact",
        action="store_true",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )

    validate = sub.add_parser("validate", parents=[output_parent], help="仅校验样本，不创建数据库")
    validate.add_argument("bundle")
    validate.set_defaults(func=cmd_validate)

    init = sub.add_parser("init", parents=[output_parent], help="校验并初始化 SQLite 状态")
    init.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    init.add_argument("--db", default=str(DEFAULT_DB))
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    common_engine = argparse.ArgumentParser(add_help=False)
    common_engine.add_argument(
        "--compact",
        action="store_true",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    common_engine.add_argument("--db", default=str(DEFAULT_DB))
    common_engine.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    common_engine.add_argument("--ensure-demo", action="store_true", help="数据库不存在时自动初始化演示样本")

    step = sub.add_parser("step", parents=[common_engine], help="提交一个编排轮次")
    step.add_argument("--round", type=int)
    step.add_argument("--clock-ms", type=int)
    step.add_argument("--request-id")
    step.add_argument("--expected-version", type=int)
    step.set_defaults(func=cmd_step)

    run = sub.add_parser("run", parents=[common_engine], help="连续提交多个轮次")
    run.add_argument("--rounds", type=int, default=5)
    run.set_defaults(func=cmd_run)

    state = sub.add_parser("state", parents=[common_engine], help="读取已提交快照")
    state.add_argument("--round", type=int)
    state.add_argument("--full", action="store_true")
    state.set_defaults(func=cmd_state)

    errors = sub.add_parser("errors", parents=[common_engine], help="读取业务错误链")
    errors.add_argument("--limit", type=int, default=100)
    errors.set_defaults(func=cmd_errors)

    events = sub.add_parser("events", parents=[common_engine], help="读取队列生命周期事件")
    events.add_argument("--round", type=int)
    events.add_argument("--limit", type=int, default=200)
    events.set_defaults(func=cmd_events)

    recover = sub.add_parser("recover", parents=[common_engine], help="重放未提交轮次作业")
    recover.set_defaults(func=cmd_recover)

    submit = sub.add_parser("submit", parents=[common_engine], help="提交单个新申请")
    submit.add_argument("--seq", type=int, required=True)
    submit.add_argument("--holder", required=True)
    submit.add_argument("--lock-key", required=True)
    submit.add_argument("--arrival-ms", type=int, required=True)
    submit.add_argument("--desired-hold-ms", type=int, required=True)
    submit.add_argument("--label", required=True)
    submit.add_argument("--priority", type=int)
    submit.add_argument("--expected-generation", type=int)
    submit.add_argument("--expected-version", type=int)
    submit.set_defaults(func=cmd_submit)

    cancel = sub.add_parser("cancel", parents=[common_engine], help="撤销或放弃待授予申请")
    cancel.add_argument("--seq", type=int, required=True)
    cancel.add_argument("--abandon", action="store_true")
    cancel.add_argument("--reason", default="operator_cancelled")
    cancel.set_defaults(func=cmd_cancel)

    for name, handler in (("serve", cmd_serve), ("demo", cmd_demo)):
        command = sub.add_parser(name, parents=[common_engine], help="启动本地交互式面板" if name == "serve" else "重置样本并启动面板")
        command.add_argument("--host", default="127.0.0.1")
        command.add_argument("--port", type=int, default=8765)
        command.add_argument("--open", action="store_true")
        if name == "serve":
            command.add_argument("--reset", action="store_true")
        else:
            command.add_argument("--headless", action="store_true")
        command.set_defaults(func=handler)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BusinessError as exc:
        payload = exc.to_dict()
        if exc.round_no is not None:
            payload["error"]["round_no"] = exc.round_no
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": str(exc),
                        "round_no": None,
                        "lock_key": None,
                        "application_seq": None,
                        "constraint": None,
                        "details": {"type": type(exc).__name__},
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
