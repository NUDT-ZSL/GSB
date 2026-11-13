"""命令行入口。

用法：
    python -m wav_splitter --wav 输入.wav --csv 标记.csv --output 输出目录

设计要点：
- 先完成 CSV 与 WAV 的全部校验，再创建输出目录、写任何文件，
  因此输入非法时输出目录保持原样（无新文件产生）
- 标记级问题（越界/无效/重叠）只跳过该片段并向 stderr 输出警告，
  整体运行仍成功（退出码 0）；输入级致命错误退出码为 2
"""

from __future__ import annotations

import argparse
import os
import sys

from .errors import SplitterError
from .markers import load_markers
from .models import STATUS_SUCCESS
from .processor import process_markers
from .report import build_report, write_report
from .wav_io import load_wav


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wav_splitter",
        description="按 CSV 标记把单声道 PCM WAV 剪辑成多个片段并生成 JSON 报告",
    )
    parser.add_argument("--wav", "-w", required=True, help="输入单声道 PCM WAV 路径")
    parser.add_argument("--csv", "-c", required=True, help="输入 CSV 标记文件路径（UTF-8，三列）")
    parser.add_argument("--output", "-o", required=True, help="输出目录（不存在时自动创建）")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ---- 阶段 1：输入校验（任何输出文件产生之前）----
    try:
        markers = load_markers(args.csv)
        info, pcm = load_wav(args.wav)
    except SplitterError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    # ---- 阶段 2：准备输出目录 ----
    try:
        os.makedirs(args.output, exist_ok=True)
    except OSError as exc:
        print(f"错误：输出目录无法创建：{args.output}（{exc}）", file=sys.stderr)
        return 2

    # ---- 阶段 3：逐标记处理（跳过项写 stderr 警告）----
    try:
        results = process_markers(markers, info, pcm, args.output)
        report = build_report(results, info, args.wav, args.csv)
        report_path = write_report(report, args.output)
    except SplitterError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    success = sum(1 for r in results if r.status == STATUS_SUCCESS)
    skipped = len(results) - success
    print(
        f"处理完成：共 {len(results)} 个标记，成功导出 {success} 个，"
        f"跳过 {skipped} 个；报告：{report_path}"
    )
    return 0


def main() -> None:
    sys.exit(run())
