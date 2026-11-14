"""命令行入口（需求 10）。

用法：
    python run_handover.py --input-dir <输入目录> \
        --config <映射配置.json> --output-dir <输出目录>

退出码：
- 0：处理完成（报告中可能仍有“未完成”文件，属于业务结果而非执行失败）；
- 2：配置错误（需求 9：整体拒绝，不产生半成品）；
- 1：其他致命错误（如输入目录不存在、状态文件损坏）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Windows 控制台可能不是 UTF-8，统一调整，保证中文报告路径输出不乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from .config import load_config
from .errors import ConfigError, PipelineError
from .pipeline import (
    Pipeline,
    REPORT_JSON_NAME,
    REPORT_MD_NAME,
)
from .report import write_reports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="handover",
        description="多来源 JSONL 业务记录交接规范化工具",
    )
    parser.add_argument("--input-dir", required=True,
                        help="存放来源 .jsonl 文件的目录")
    parser.add_argument("--config", required=True,
                        help="字段映射配置 JSON 文件路径")
    parser.add_argument("--output-dir", required=True,
                        help="规范记录与交接报告的输出目录")
    parser.add_argument("--run-at", default=None,
                        help="覆盖报告生成时间（仅用于可重复演示，格式为 ISO 8601）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # 需求 9：先完整校验配置，任何错误都在创建输出目录之前中止
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2

    try:
        pipeline = Pipeline(
            config,
            Path(args.input_dir),
            Path(args.output_dir),
            run_at=args.run_at,
        )
        report = pipeline.run()
        write_reports(
            report,
            Path(args.output_dir) / REPORT_JSON_NAME,
            Path(args.output_dir) / REPORT_MD_NAME,
        )
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 2
    except PipelineError as exc:
        print(f"[执行错误] {exc}", file=sys.stderr)
        return 1

    s = report["summary"]
    print("处理完成：")
    print(f"  规范记录 {s['canonical_records']} 条，"
          f"拒绝 {s['records_rejected_missing_required']} 条，"
          f"合并 {s['duplicate_merge_events']} 起，"
          f"冲突裁决 {s['field_conflict_events']} 起")
    print(f"  完整可用文件 {s['files_complete']} 个，"
          f"未完成文件 {s['files_unfinished']} 个")
    print(f"  产物目录：{Path(args.output_dir).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
