"""命令行入口：读取清单，输出预览帧 PNG 序列与说明 JSON。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .models import InputError, load_manifest
from .render import OutputError, render

EXIT_INPUT = 2
EXIT_OUTPUT = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subtitle-preview",
        description="把字幕按安全区与面板约束叠加到过场动画帧上，输出 PNG 预览序列和说明 JSON。",
    )
    parser.add_argument("--manifest", "-m", required=True, help="清单 JSON 路径")
    parser.add_argument("--out", "-o", required=True, help="输出目录（不存在时自动创建）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        report = render(manifest, Path(args.out))
    except InputError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return EXIT_INPUT
    except OutputError as exc:
        print(f"输出错误：{exc}", file=sys.stderr)
        return EXIT_OUTPUT
    except OSError as exc:
        print(f"文件读写失败：{exc}", file=sys.stderr)
        return EXIT_OUTPUT

    summary = report["summary"]
    print(
        f"完成：{summary['rendered_frame_count']}/{summary['frame_count']} 帧已输出，"
        f"字幕 {summary['subtitle_count']} 条（已放置 {summary['placed_count']}，"
        f"未放置 {summary['unplaced_count']}，偏移过大 {summary['large_onset_offset_count']}），"
        f"问题帧 {summary['missing_or_broken_frame_count']} 个"
    )
    for warning in report["warnings"]:
        print(f"提示：{warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
