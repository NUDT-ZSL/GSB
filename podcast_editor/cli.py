"""命令行入口。

用法::

    python -m podcast_editor.cli --instructions edits.json \
        --output out/episode.wav --report out/report.json [--fresh]

退出码：
- 0  全部指令成功（complete）或指令数组为空（empty）
- 2  部分失败但已产出可用 WAV（incomplete）
- 3  完全失败，无任何音频产出（failed）
- 1  指令文件本身无法解析等致命错误
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .engine import EditorEngine
from .errors import EditError, ErrorCode
from .report import build_report, write_report


def parse_instructions(path: str) -> list:
    """读取并校验指令 JSON 文件的顶层结构。"""
    if not os.path.exists(path):
        raise EditError(
            ErrorCode.INSTRUCTION_FILE_INVALID,
            f"指令文件不存在: {path}",
        )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise EditError(
            ErrorCode.INSTRUCTION_FILE_INVALID,
            f"指令文件不是合法 JSON: {path} ({exc})",
        ) from exc
    except OSError as exc:
        raise EditError(
            ErrorCode.INSTRUCTION_FILE_INVALID,
            f"指令文件无法读取: {path} ({exc})",
        ) from exc

    if not isinstance(data, list):
        raise EditError(
            ErrorCode.INSTRUCTION_FILE_INVALID,
            "指令文件顶层必须是 JSON 数组（每条指令一个对象）",
        )
    return data


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="播客单声道 16-bit PCM WAV 批量编辑工具（裁剪/拼接/音量）"
    )
    parser.add_argument(
        "-i", "--instructions", required=True, help="编辑指令 JSON 文件路径"
    )
    parser.add_argument(
        "-o", "--output", required=True, help="输出合并 WAV 路径"
    )
    parser.add_argument(
        "-r", "--report", required=True, help="编辑报告 JSON 输出路径"
    )
    parser.add_argument(
        "--work-dir",
        default=None,
        help="检查点目录（默认与报告同目录），用于失败指令的断点续跑",
    )
    parser.add_argument(
        "--base-dir",
        default=None,
        help="源文件相对路径的解析基准目录（默认取指令文件所在目录）",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="忽略已有检查点，清空时间线全新运行",
    )
    args = parser.parse_args(argv)

    try:
        instructions = parse_instructions(args.instructions)
    except EditError as exc:
        print(f"致命错误 [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1

    base_dir = args.base_dir or os.path.dirname(os.path.abspath(args.instructions))
    base_dir = os.path.abspath(base_dir)
    work_dir = args.work_dir or os.path.dirname(os.path.abspath(args.report))

    engine = EditorEngine(
        instructions=instructions,
        base_dir=base_dir,
        work_dir=work_dir,
        output_wav=args.output,
        fresh=args.fresh,
    )
    engine.run()

    report = build_report(
        engine,
        instruction_file=args.instructions,
        output_wav=args.output,
        resumed=getattr(engine, "resumed", False),
    )
    write_report(report, args.report)

    status = report["overall_status"]
    n_ok = report["summary"]["succeeded"]
    n_fail = report["summary"]["failed"]
    print(
        f"处理完成: 状态={status} 成功={n_ok} 失败={n_fail} "
        f"时长={report['summary']['total_duration_seconds']:.3f}s "
        f"报告={args.report}"
    )
    if report["resumed_from_checkpoint"]:
        print("本次为断点续跑：历史成功指令已跳过，仅重试此前失败的指令。")

    return {"complete": 0, "empty": 0, "incomplete": 2, "failed": 3}[status]


if __name__ == "__main__":
    sys.exit(main())
