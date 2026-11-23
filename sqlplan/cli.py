from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any, Optional, Sequence

from . import __version__
from .errors import BusinessError, ErrorCode
from .service import AppService
from .util import pretty_json


def _common_options(*, suppress_defaults: bool) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--db",
        default=default if suppress_defaults else "runtime/state.db",
        help="SQLite 状态库路径",
    )
    parser.add_argument(
        "--lock",
        default=default if suppress_defaults else "runtime/app.lock",
        help="写入文件锁路径",
    )
    parser.add_argument(
        "--logs",
        default=default if suppress_defaults else "runtime/logs",
        help="JSONL 日志目录",
    )
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=default if suppress_defaults else 15.0,
        help="文件锁等待秒数",
    )
    return parser


def _add_sample_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--views", default="views.json", help="视图定义 JSON")
    parser.add_argument("--v1", default="snapshots/v1.json", help="v1 快照 JSON")
    parser.add_argument("--v2-dir", default="snapshots/v2", help="v2 分片目录")
    parser.add_argument(
        "--v2-meta", default=None, help="v2 SQLite 元数据（默认位于 v2 目录）"
    )
    parser.add_argument(
        "--base-clock",
        type=int,
        default=None,
        help="v1 相对秒换算使用的注入逻辑时钟毫秒",
    )


def build_parser() -> argparse.ArgumentParser:
    root_common = _common_options(suppress_defaults=False)
    root = argparse.ArgumentParser(
        prog="sqlplan",
        description="SQL 视图逻辑计划、增量失效与两代快照操作工具",
        parents=[root_common],
    )
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command", required=True)
    child_common = _common_options(suppress_defaults=True)

    load_parser = sub.add_parser(
        "load", help="校验并加载视图与两代快照", parents=[child_common]
    )
    _add_sample_args(load_parser)
    load_parser.add_argument(
        "--decision",
        choices=["incremental_append", "complete_replace"],
        default=None,
        help="覆盖自动快照包判定（仅用于测试）",
    )

    edit_parser = sub.add_parser(
        "edit", help="提交视图编辑补丁", parents=[child_common]
    )
    edit_parser.add_argument("patch", help="补丁 JSON 路径；- 表示标准输入")

    plan_parser = sub.add_parser(
        "plan", help="输出当前代际的逻辑计划", parents=[child_common]
    )
    plan_parser.add_argument("--view", default=None, help="只输出指定视图")

    sub.add_parser(
        "resume", help="恢复已准备或已 staged 的运行", parents=[child_common]
    )
    sub.add_parser(
        "status", help="稳定输出当前计划代际与失效事件状态", parents=[child_common]
    )

    verify_parser = sub.add_parser(
        "verify", help="校验状态库、计划确定性与样本快照", parents=[child_common]
    )
    _add_sample_args(verify_parser)
    verify_parser.add_argument(
        "--no-sample",
        action="store_true",
        help="只校验状态库，不重新校验样本文件",
    )
    return root


def _service_from_args(args: argparse.Namespace) -> AppService:
    return AppService(
        db_path=getattr(args, "db", "runtime/state.db"),
        lock_path=getattr(args, "lock", "runtime/app.lock"),
        log_dir=getattr(args, "logs", "runtime/logs"),
        lock_timeout_seconds=getattr(args, "lock_timeout", 15.0),
    )


def _read_stdin_patch() -> Any:
    try:
        return json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            ErrorCode.E_BAD_JSON,
            "标准输入不是合法 JSON",
            {"line": exc.lineno, "column": exc.colno, "reason": exc.msg},
        ) from exc


def run(args: argparse.Namespace) -> int:
    service = _service_from_args(args)
    if args.command == "load":
        result = service.load(
            views_path=args.views,
            v1_path=args.v1,
            v2_dir=args.v2_dir,
            v2_meta_path=args.v2_meta,
            injected_clock_ms=args.base_clock,
            force_decision=args.decision,
        )
        print(pretty_json(result))
        return 0
    if args.command == "edit":
        patch: Any
        if args.patch == "-":
            patch = _read_stdin_patch()
        else:
            patch = args.patch
        result = service.edit(patch)
        print(pretty_json(result))
        return 0
    if args.command == "plan":
        print(pretty_json(service.plan(args.view)))
        return 0
    if args.command == "resume":
        print(pretty_json(service.resume()))
        return 0
    if args.command == "status":
        print(pretty_json(service.status()))
        return 0
    if args.command == "verify":
        sample_args = {}
        if not args.no_sample:
            sample_args = {
                "sample_views_path": args.views,
                "sample_v1_path": args.v1,
                "sample_v2_dir": args.v2_dir,
                "sample_v2_meta_path": args.v2_meta,
            }
        result = service.verify(**sample_args)
        print(pretty_json(result))
        return 0 if result["ok"] else 1
    raise BusinessError(ErrorCode.E_INTERNAL, "未知命令", {"command": args.command})


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except BusinessError as exc:
        print(pretty_json(exc.to_dict()))
        return exc.exit_code
    except KeyboardInterrupt:
        print(pretty_json(BusinessError(ErrorCode.E_INTERNAL, "操作被中断").to_dict()))
        return 130
    except Exception as exc:  # pragma: no cover - last-resort diagnostic path
        payload = BusinessError(
            ErrorCode.E_INTERNAL,
            "未处理的内部异常",
            {
                "exception": type(exc).__name__,
                "reason": str(exc),
                "traceback": traceback.format_exc().splitlines()[-12:],
            },
        )
        print(pretty_json(payload.to_dict()))
        return payload.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
