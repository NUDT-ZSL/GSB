"""命令行入口：python -m audition_plan --events ... --clips ... --out ..."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import InputError, plan_audition, write_outputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audition_plan",
        description="为技能冷却事件匹配音效片段并生成带时间轴的试听清单。",
    )
    parser.add_argument("--events", required=True, help="事件表 JSON 路径")
    parser.add_argument("--clips", required=True, help="片段清单 JSON 路径")
    parser.add_argument("--out", required=True, help="输出目录（不存在时自动创建）")
    return parser


def _print_summary(result, csv_path: Path, report_path: Path) -> None:
    summary = result.summary()
    print(
        f"事件 {summary['event_count']} 个：匹配 {summary['matched_count']}，"
        f"缺失 {summary['missing_event_count']}，重叠警告 {summary['overlap_warning_count']}，"
        f"不可用片段 {summary['unusable_clip_count']}"
    )
    for row in result.rows:
        if row.clip is None:
            print(f"  缺失 event={row.event.event_id}: {row.missing_reason}")
    for entry in result.unusable_clips:
        print(f"  不可用片段 {entry['clip_id']}: {entry['reason']}")
    print(f"CSV:    {csv_path}")
    print(f"报告:   {report_path}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = plan_audition(args.events, args.clips)
        csv_path, report_path = write_outputs(result, args.out)
    except InputError as exc:
        print(f"输入错误: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"输出错误: {exc}", file=sys.stderr)
        return 1
    _print_summary(result, csv_path, report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
