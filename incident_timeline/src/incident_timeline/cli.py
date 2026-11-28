"""argparse 命令行入口。"""
from __future__ import annotations

import argparse
import json
import sys

from .export import export_run
from .pipeline import DEFAULT_PARAMS, run_full, run_incremental
from .registry import load_registry
from .storage import connect


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="incident-timeline",
        description="多源事件归一、故障聚类、时间线与数量账目汇总（仅使用 Python 标准库）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="执行全量或增量汇总并写入 SQLite")
    run_parser.add_argument("--events", required=True, help="JSONL 多源事件文件路径")
    run_parser.add_argument("--registry", required=True, help="依赖登记 JSON 路径")
    run_parser.add_argument("--db", required=True, help="SQLite 数据库文件路径")
    run_parser.add_argument(
        "--incremental-batch",
        default=None,
        help="增量批次标识；提供时执行增量重算，缺省执行全量汇总",
    )
    run_parser.add_argument(
        "--storm-window-ms", type=int, default=DEFAULT_PARAMS["storm_window_ms"],
        help="告警风暴去重窗口（毫秒）",
    )
    run_parser.add_argument(
        "--proximity-window-ms", type=int, default=DEFAULT_PARAMS["proximity_window_ms"],
        help="同服务候选簇邻近窗口（毫秒）",
    )
    run_parser.add_argument(
        "--merge-window-ms", type=int, default=DEFAULT_PARAMS["merge_window_ms"],
        help="跨服务依赖合并窗口（毫秒）",
    )
    run_parser.add_argument("--export-dir", default=None, help="运行后导出目录（可选）")

    export_parser = sub.add_parser("export", help="从 SQLite 导出指定运行的时间线与账目")
    export_parser.add_argument("--registry", required=True, help="依赖登记 JSON 路径（校验用）")
    export_parser.add_argument("--db", required=True, help="SQLite 数据库文件路径")
    export_parser.add_argument("--out-dir", required=True, help="导出目录")
    export_parser.add_argument("--run-id", type=int, default=None, help="运行编号，缺省取最新一次")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "export":
        load_registry(args.registry)
        connection = connect(args.db)
        try:
            paths = export_run(connection, args.out_dir, args.run_id)
        finally:
            connection.close()
        print(json.dumps(paths, ensure_ascii=False, indent=2))
        return 0

    registry = load_registry(args.registry)
    params = {
        "storm_window_ms": args.storm_window_ms,
        "proximity_window_ms": args.proximity_window_ms,
        "merge_window_ms": args.merge_window_ms,
    }
    connection = connect(args.db)
    try:
        if args.incremental_batch:
            result = run_incremental(
                args.events, registry, params, args.incremental_batch, connection
            )
        else:
            result = run_full(args.events, registry, params, "batch-full")
        run_id = _persist(connection, result)
        export_paths = None
        if args.export_dir:
            export_paths = export_run(connection, args.export_dir, run_id)
    finally:
        connection.close()

    print(
        json.dumps(
            {
                "run_id": run_id,
                "mode": result["mode"],
                "summary": result["summary"],
                "exports": export_paths,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not result["accounts"]["reconciliation"]["all_ok"]:
        print("数量账目对账失败", file=sys.stderr)
        return 2
    if not result["timeline"]["consistent"]:
        print("时间线存在矛盾，请检查 timeline_conflicts", file=sys.stderr)
        return 3
    return 0


def _persist(connection, result) -> int:
    from .storage import save_run

    return save_run(connection, result)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1)
