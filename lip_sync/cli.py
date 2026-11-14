"""Command-line entry point.

Exit codes
----------
0  run completed and the report was written (even if there are zero valid lines)
2  a required argument is missing or invalid
3  an input document is missing / unreadable / not valid JSON
4  the report could not be written
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .inputs import InputError, load_dialogues, load_frame_table
from .verify import (
    AudioProbeCache,
    build_report,
    format_console_summary,
    verify_all,
    write_report,
)

REPORT_FILENAME = "lip_sync_report.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lip-sync-check",
        description="离线核对每句对白的口型时间与 WAV 实际起音点之间的偏差。",
    )
    parser.add_argument("--frames", required=True, help="帧表 JSON 路径（含语句标识与毫秒口型时间）")
    parser.add_argument("--dialogues", required=True, help="对白清单 JSON 路径（含语句标识与 WAV 路径）")
    parser.add_argument("--tolerance-ms", required=True, type=float, help="超差容差，单位毫秒，需 >= 0")
    parser.add_argument("--out-dir", required=True, help="输出目录，结论 JSON 将写入该目录")
    parser.add_argument(
        "--report-name",
        default=REPORT_FILENAME,
        help=f"输出文件名（默认 {REPORT_FILENAME}）",
    )
    parser.add_argument("--quiet", action="store_true", help="不在终端打印逐句表格")
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not (args.tolerance_ms >= 0.0):
        parser.error("--tolerance-ms 必须是 >= 0 的数值")
    frames = Path(args.frames)
    if not frames.is_file():
        parser.error(f"--frames 指向的文件不存在: {frames}")
    dialogues = Path(args.dialogues)
    if not dialogues.is_file():
        parser.error(f"--dialogues 指向的文件不存在: {dialogues}")
    out_dir = Path(args.out_dir)
    if out_dir.exists() and not out_dir.is_dir():
        parser.error(f"--out-dir 已存在但不是目录: {out_dir}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    try:
        frames = load_frame_table(args.frames)
        dialogues = load_dialogues(args.dialogues)
    except InputError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 3

    cache = AudioProbeCache()
    result = verify_all(frames, dialogues, args.tolerance_ms, cache)
    report = build_report(result)
    report["wav_files_decoded"] = cache.decode_count

    out_path = Path(args.out_dir) / args.report_name
    try:
        write_report(report, out_path)
    except OSError as exc:
        print(f"错误: 无法写出结论文件 {out_path} ({exc})", file=sys.stderr)
        return 4

    if not args.quiet:
        print(format_console_summary(report))
        print()
    print(f"结论已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
