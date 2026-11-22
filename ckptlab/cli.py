from __future__ import annotations

import argparse
import ipaddress
import sys
from pathlib import Path
from typing import Any

from .errors import BusinessError
from .manager import CheckpointManager
from .util import load_json_object, pretty_json


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, help="SQLite 账本路径")
    parser.add_argument("--store", required=True, help="分片与清单存储目录")


def _add_version(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--version", type=int, default=None, help="指定已提交版本")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ckptlab",
        description="确定性分片检查点管理与诊断工具",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    save = subparsers.add_parser("save", help="执行一轮并行落盘")
    _add_common(save)
    save.add_argument("--plan", required=True)
    save.add_argument("--shards", required=True)
    save.add_argument("--crash-plan", default=None)

    restore = subparsers.add_parser("restore", help="恢复并裁决完整检查点")
    _add_common(restore)
    _add_version(restore)
    restore.add_argument("--output-dir", default=None)

    resume = subparsers.add_parser("resume", help="恢复未完成落盘轮次")
    _add_common(resume)
    resume.add_argument("--plan", default=None)
    resume.add_argument("--shards", default=None)
    resume.add_argument("--crash-plan", default=None)
    resume.add_argument("--logical-clock-ms", type=int, default=None)
    resume.add_argument("--max-concurrency", type=int, default=None)
    resume.add_argument("--logical-time-budget-ms", type=int, default=None)
    resume.add_argument("--logical-clock-tolerance-ms", type=int, default=None)
    incremental = resume.add_mutually_exclusive_group()
    incremental.add_argument("--incremental", dest="incremental", action="store_true")
    incremental.add_argument(
        "--no-incremental", dest="incremental", action="store_false"
    )
    resume.set_defaults(incremental=None)

    status = subparsers.add_parser("status", help="输出稳定排序诊断快照")
    _add_common(status)
    _add_version(status)

    verify = subparsers.add_parser("verify", help="校验清单、分片与独立重建一致性")
    _add_common(verify)
    _add_version(verify)

    serve = subparsers.add_parser("serve", help="启动回环诊断工作台")
    _add_common(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    return parser


def _load_optional(path: str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    return load_json_object(path, kind=Path(path).name)


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    manager = CheckpointManager(args.db, args.store)
    if args.command == "save":
        plan = load_json_object(args.plan, kind="training_plan")
        policy = load_json_object(args.shards, kind="shard_policy")
        crash = _load_optional(args.crash_plan)
        return manager.save(plan, policy, crash)
    if args.command == "resume":
        if (args.plan is None) != (args.shards is None):
            raise BusinessError(
                "TRAINING_PLAN_INVALID",
                "resume 的 --plan 与 --shards 必须同时提供或同时省略",
            )
        plan = _load_optional(args.plan)
        policy = _load_optional(args.shards)
        crash = _load_optional(args.crash_plan)
        overrides = {
            "logical_clock_ms": args.logical_clock_ms,
            "max_concurrency": args.max_concurrency,
            "logical_time_budget_ms": args.logical_time_budget_ms,
            "logical_clock_tolerance_ms": args.logical_clock_tolerance_ms,
            "incremental": args.incremental,
        }
        return manager.resume(plan, policy, crash, overrides)
    if args.command == "restore":
        return manager.restore(args.version, args.output_dir)
    if args.command == "status":
        return manager.status(args.version)
    if args.command == "verify":
        return manager.verify(args.version)
    if args.command == "serve":
        from .web import create_app

        host = args.host
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host == "localhost"
        if not is_loopback:
            raise BusinessError(
                "WEB_HOST_NOT_LOOPBACK",
                "Flask 诊断工作台只允许监听回环地址",
                {"host": host},
            )
        app = create_app(args.db, args.store)
        app.run(host=host, port=args.port, debug=False, use_reloader=False)
        return {"ok": True, "command": "serve", "host": host, "port": args.port}
    raise BusinessError("INPUT_JSON_INVALID", "未知命令", command=args.command)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = _dispatch(args)
        print(pretty_json(result))
        if args.command in {"save", "resume"} and not result.get("committed", False):
            return 3
        return 0
    except BusinessError as exc:
        print(pretty_json(exc.as_dict()), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        raise


if __name__ == "__main__":
    raise SystemExit(main())
