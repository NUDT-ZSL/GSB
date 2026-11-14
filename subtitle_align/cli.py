"""命令行入口：python -m subtitle_align.cli <字幕表.json> [选项]。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Windows 控制台默认 GBK，强制 UTF-8 输出避免中文/特殊符号编码失败
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 兼容 `python -m subtitle_align.cli` 与 `python subtitle_align/cli.py` 两种启动：
# 直接运行脚本时把项目根加入 sys.path，再改用绝对导入
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from subtitle_align.audit import audit
    from subtitle_align.errors import SubtitleInputError
    from subtitle_align.loader import load_subtitle_table
    from subtitle_align.preview import render_preview
    from subtitle_align.reporter import format_summary, write_report
else:
    from .audit import audit
    from .errors import SubtitleInputError
    from .loader import load_subtitle_table
    from .preview import render_preview
    from .reporter import format_summary, write_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subtitle_align",
        description="双语字幕对齐核对工具：生成逐句核对报告 JSON 与对齐预览图 PNG。",
    )
    parser.add_argument("input", help="字幕表 JSON 文件路径")
    parser.add_argument(
        "-r", "--report",
        help="核对报告输出路径（默认：输入同目录下 <输入名>.report.json）",
    )
    parser.add_argument(
        "-p", "--preview",
        help="预览图输出路径（默认：输入同目录下 <输入名>.preview.png）",
    )
    return parser


def run(input_path: str, report_path: str | None = None,
        preview_path: str | None = None) -> int:
    """供 CLI 与测试脚本复用的主流程，返回进程退出码。"""
    src = Path(input_path)
    report_dest = Path(report_path) if report_path else src.with_suffix(".report.json")
    preview_dest = Path(preview_path) if preview_path else src.with_suffix(".preview.png")

    # 校验失败时直接抛错；此刻尚未写任何输出文件
    table = load_subtitle_table(src)
    report = audit(table)
    png_bytes = render_preview(report)

    # 渲染成功后再落盘，保证出错时不产生半成品
    write_report(report, report_dest)
    preview_dest.parent.mkdir(parents=True, exist_ok=True)
    preview_dest.write_bytes(png_bytes)

    print(format_summary(report))
    print(f"核对报告：{report_dest}")
    print(f"预览图：  {preview_dest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args.input, args.report, args.preview)
    except SubtitleInputError as exc:
        # 明确错误并停止：退出码 2，不生成预览图/报告
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
