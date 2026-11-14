"""命令行入口（需求 1、10、11）。

用法：
    python -m subtitle_audit.cli shots.json subtitles.json [--csv 路径] [--png 路径]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import ManifestError
from .model import (
    audit,
    check_shot_conflicts,
    load_shots,
    load_subtitles,
    summarize,
)
from .report import render_png, write_csv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subtitle_audit",
        description="对白镜头字幕校验：归属/越界/重叠检查，输出明细 CSV 与对照 PNG。",
    )
    parser.add_argument("shots", help="镜头清单 JSON 路径")
    parser.add_argument("subtitles", help="字幕清单 JSON 路径")
    parser.add_argument(
        "--csv", dest="csv_path", default=None,
        help="明细 CSV 输出路径（默认：out/report.csv）",
    )
    parser.add_argument(
        "--png", dest="png_path", default=None,
        help="对照 PNG 输出路径（默认：out/timeline.png）",
    )
    return parser


def _force_utf8_console() -> None:
    """Windows 控制台默认可能是 GBK 代码页，统一改用 UTF-8 输出，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    args = build_parser().parse_args(argv)
    csv_path = Path(args.csv_path) if args.csv_path else Path("out") / "report.csv"
    png_path = Path(args.png_path) if args.png_path else Path("out") / "timeline.png"

    # 需求 1：字段/类型错误指明具体文件与键名，非零退出
    try:
        shots = load_shots(args.shots)
        subs = load_subtitles(args.subtitles)
    except ManifestError as exc:
        print(f"输入清单错误：{exc}", file=sys.stderr)
        return 2

    # 需求 8：镜头乱序/重叠警告，继续处理不冲突镜头
    warnings = check_shot_conflicts(shots)
    for msg in warnings:
        print(msg, file=sys.stderr)

    rows = audit(shots, subs)

    # 需求 10：目录不存在自动创建
    write_csv(rows, csv_path)
    render_png(rows, shots, png_path)

    # 需求 11：摘要 + 退出码 0
    counts = summarize(rows)
    print("字幕校验摘要：")
    print(f"有效 {counts['有效']}")
    print(f"越界 {counts['越界']}")
    print(f"重叠 {counts['重叠']}")
    print(f"无效 {counts['无效']}")
    print(f"明细 CSV：{csv_path}")
    print(f"对照 PNG：{png_path}")
    if warnings:
        print(f"镜头清单警告 {len(warnings)} 条（详见标准错误输出）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
