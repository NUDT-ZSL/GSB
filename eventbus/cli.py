"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .engine import EventBus
from .errors import BusinessError, err
from .util import read_json
from .validation import load_dataset


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise err("PATCH_INVALID", f"参数错误: {message}")


def _add_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        help="SQLite 状态数据库路径（默认 data/eventbus.sqlite）",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="eventbus")
    _add_db(parser)
    parser.set_defaults(db="data/eventbus.sqlite")
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="校验并加载两代样本")
    _add_db(load_parser)
    load_parser.add_argument("--v1", required=True)
    load_parser.add_argument("--v2-shards", required=True)
    load_parser.add_argument("--v2-metadata", required=True)
    load_parser.add_argument("--replace", action="store_true")
    load_parser.add_argument("--clock-ms", type=int)
    load_parser.add_argument("--retention-bytes", type=int)
    load_parser.add_argument("--clock-tolerance-ms", type=int)

    edit_parser = subparsers.add_parser("edit", help="以补丁发布配置新版本")
    _add_db(edit_parser)
    edit_parser.add_argument("--patch", help="补丁 JSON 文件")
    edit_parser.add_argument("--patch-json", help="补丁 JSON 文本")
    edit_parser.add_argument("--clock-ms", type=int)
    edit_parser.add_argument("--actor", default="edit")

    publish_parser = subparsers.add_parser(
        "publish", help="追加事件或切换配置版本"
    )
    _add_db(publish_parser)
    publish_parser.add_argument("--version", type=int)
    publish_parser.add_argument("--channel")
    publish_parser.add_argument("--events")
    publish_parser.add_argument("--events-json")
    publish_parser.add_argument("--max-batch", type=int)
    publish_parser.add_argument("--clock-ms", type=int)

    bridge_parser = subparsers.add_parser("bridge", help="执行桥接转发")
    _add_db(bridge_parser)
    bridge_parser.add_argument("bridge_id", nargs="?")
    bridge_parser.add_argument("--bridge", dest="bridge_option")
    bridge_parser.add_argument("--all", action="store_true")
    bridge_parser.add_argument("--max-events", type=int)
    bridge_parser.add_argument("--max-runs", type=int, default=1000)
    bridge_parser.add_argument("--clock-ms", type=int)
    bridge_parser.add_argument("--report-offset", type=int)
    bridge_parser.add_argument("--report-digest")
    bridge_parser.add_argument("--config-version", type=int)

    consume_parser = subparsers.add_parser("consume", help="消费并提交订阅偏移")
    _add_db(consume_parser)
    consume_parser.add_argument("--subscription", required=True)
    consume_parser.add_argument("--count", type=int)
    consume_parser.add_argument("--to-offset", type=int)
    consume_parser.add_argument("--request-id")
    consume_parser.add_argument("--clock-ms", type=int)

    cleanup_parser = subparsers.add_parser(
        "cleanup", help="按保留上限与读者最小位点执行清理裁决"
    )
    _add_db(cleanup_parser)
    cleanup_parser.add_argument("--clock-ms", type=int)
    cleanup_parser.add_argument("--retention-bytes", type=int)

    resume_parser = subparsers.add_parser("resume", help="恢复未完成桥接意图")
    _add_db(resume_parser)

    status_parser = subparsers.add_parser("status", help="输出稳定状态 JSON")
    _add_db(status_parser)

    verify_parser = subparsers.add_parser("verify", help="重放并校验持久水位")
    _add_db(verify_parser)

    serve_parser = subparsers.add_parser("serve", help="启动本地 Flask 接口")
    _add_db(serve_parser)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=5000)
    return parser


def _dispatch(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    bus = EventBus(args.db)
    if args.command == "load":
        dataset = load_dataset(
            args.v1,
            args.v2_shards,
            args.v2_metadata,
            retention_bytes=args.retention_bytes,
            clock_tolerance_ms=args.clock_tolerance_ms,
        )
        return (
            bus.load(dataset, replace=args.replace, clock_ms=args.clock_ms),
            0,
        )
    if args.command == "edit":
        if bool(args.patch) == bool(args.patch_json):
            raise err("PATCH_INVALID", "--patch 与 --patch-json 必须且只能指定一个")
        patch = read_json(args.patch) if args.patch else _parse_inline_json(args.patch_json)
        return bus.edit(patch, clock_ms=args.clock_ms, actor=args.actor), 0
    if args.command == "publish":
        if args.version is not None:
            if args.channel or args.events or args.events_json:
                raise err("PATCH_INVALID", "--version 不能与事件追加参数同时使用")
            return bus.publish_version(args.version, clock_ms=args.clock_ms), 0
        if not args.channel:
            raise err("PATCH_INVALID", "事件追加必须指定 --channel")
        if bool(args.events) == bool(args.events_json):
            raise err("PATCH_INVALID", "--events 与 --events-json 必须且只能指定一个")
        document = (
            read_json(args.events)
            if args.events
            else _parse_inline_json(args.events_json)
        )
        return (
            bus.publish_events(
                args.channel,
                document,
                max_batch=args.max_batch,
                clock_ms=args.clock_ms,
            ),
            0,
        )
    if args.command == "bridge":
        explicit = [value for value in (args.bridge_id, args.bridge_option) if value]
        if len(set(explicit)) > 1:
            raise err("PATCH_INVALID", "桥接 ID 只能指定一次")
        if args.all and explicit:
            raise err("PATCH_INVALID", "--all 不能与桥接 ID 同时使用")
        if args.report_offset is not None:
            if len(explicit) != 1:
                raise err(
                    "PATCH_INVALID",
                    "位点回报必须指定且只能指定一个桥接 ID",
                )
            return (
                bus.report_bridge_position(
                    explicit[0],
                    args.report_offset,
                    expected_digest=args.report_digest,
                    config_version=args.config_version,
                ),
                0,
            )
        if args.report_digest is not None or args.config_version is not None:
            raise err(
                "PATCH_INVALID",
                "--report-digest/--config-version 只能与 --report-offset 一起使用",
            )
        status = bus.status()
        bridge_ids = (
            [item["id"] for item in status["bridges"]]
            if args.all or not explicit
            else [explicit[0]]
        )
        if explicit and explicit[0] not in {item["id"] for item in status["bridges"]}:
            raise err("NOT_FOUND", "桥接规则不存在", bridge=explicit[0])
        return (
            bus.run_bridge_cycle(
                bridge_ids,
                max_events=args.max_events,
                max_runs=args.max_runs,
                clock_ms=args.clock_ms,
            ),
            0,
        )
    if args.command == "consume":
        if (args.count is None) == (args.to_offset is None):
            raise err("PATCH_INVALID", "--count 与 --to-offset 必须且只能指定一个")
        return (
            bus.consume(
                args.subscription,
                count=args.count,
                to_offset=args.to_offset,
                request_id=args.request_id,
                clock_ms=args.clock_ms,
            ),
            0,
        )
    if args.command == "cleanup":
        return (
            bus.cleanup(
                clock_ms=args.clock_ms,
                retention_bytes=args.retention_bytes,
            ),
            0,
        )
    if args.command == "resume":
        result = bus.resume()
        return result, 0 if result["ok"] else 3
    if args.command == "status":
        return bus.status(), 0
    if args.command == "verify":
        result = bus.verify()
        return result, 0 if result["ok"] else 3
    if args.command == "serve":
        from .api import create_app

        create_app(args.db).run(host=args.host, port=args.port)
        return {"ok": True, "host": args.host, "port": args.port}, 0
    raise err("PATCH_INVALID", "未知子命令", command=args.command)


def _parse_inline_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise err(
            "INVALID_JSON",
            "内联 JSON 解析失败",
            line=exc.lineno,
            column=exc.colno,
        ) from exc


def _emit(document: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        args = build_parser().parse_args(argv)
        document, exit_code = _dispatch(args)
        _emit(document)
        return exit_code
    except BusinessError as exc:
        _emit(exc.as_dict())
        return exc.exit_code
    except KeyboardInterrupt:
        _emit(err("INTERNAL_ERROR", "操作被中断").as_dict())
        return 130
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "未处理异常",
                    "details": {"type": type(exc).__name__, "reason": str(exc)},
                },
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
