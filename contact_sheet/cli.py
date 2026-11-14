"""命令行入口。

用法：
    python -m contact_sheet.cli --video sample.mp4 --csv cuts.csv \
        --config config.json --out-dir out/
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .errors import ContactSheetError
from .pipeline import run
from .selector import DEFAULT_MERGE_GAP_MS, DEFAULT_SIMILARITY_MAE

# Windows 中文控制台默认 GBK，统一切到 UTF-8，保证中文原因可读且不抛编码异常
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contact_sheet",
        description="按切点清单从本地 MP4 生成联系表 PNG 与定位报告 JSON（不做时间点盲抽）")
    parser.add_argument("--video", required=True, help="本地视频文件路径（如 MP4）")
    parser.add_argument("--csv", required=True, help="逐帧时间戳 CSV（表头 frame,ms）")
    parser.add_argument("--config", default=None, help="联系表配置 JSON，缺省使用约定默认值")
    parser.add_argument("--out-dir", required=True, help="输出目录（不存在会自动创建）")
    parser.add_argument("--sheet-name", default="contact_sheet.png")
    parser.add_argument("--report-name", default="location_report.json")
    parser.add_argument("--merge-gap-ms", type=int, default=DEFAULT_MERGE_GAP_MS,
                        help=f"相邻镜头合并的最大时间间隔，默认 {DEFAULT_MERGE_GAP_MS}")
    parser.add_argument("--similarity-mae", type=float, default=DEFAULT_SIMILARITY_MAE,
                        help=f"画面近似判定的平均像素差阈值，默认 {DEFAULT_SIMILARITY_MAE}")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(
            video_path=args.video,
            csv_path=args.csv,
            config_path=args.config,
            out_dir=args.out_dir,
            sheet_name=args.sheet_name,
            report_name=args.report_name,
            merge_gap_ms=args.merge_gap_ms,
            similarity_mae=args.similarity_mae,
        )
    except ContactSheetError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 2

    summary = result["summary"]
    print(f"联系表已生成：{result['sheet_path']}")
    print(f"定位报告已生成：{result['report_path']}")
    print(
        f"片段 {summary['total_segments']} 条：已定位 {summary['located']}，"
        f"无法定位 {summary['unlocatable']}，被合并 {summary['merged_away']}，"
        f"清单异常 {summary['anomaly_count']} 条"
        + (f"（{summary['note']}）" if summary["note"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
