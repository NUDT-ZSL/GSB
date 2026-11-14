"""Command-line entry point.

    python -m subtitle_checker <manifest.json> <frames_dir> <output_dir>
    run_checker.cmd      <manifest.json> <frames_dir> <output_dir>

Exit codes:
    0  all checks completed (findings are reported, not errors)
    2  invalid arguments or invalid manifest (message names the field)
    3  output directory could not be written
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .manifest import ManifestError
from .pipeline import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subtitle_checker",
        description="核对竖屏短视频字幕清单：安全区越界、特效区遮挡、时间异常与重叠，输出报告与逐条预览图。",
    )
    parser.add_argument("manifest", help="字幕清单 JSON 路径")
    parser.add_argument("frames_dir", help="帧图目录（按 <id>.png 匹配）")
    parser.add_argument("output_dir", help="输出目录，不存在时自动创建")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="只输出错误信息",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        outcome = run(args.manifest, args.frames_dir, args.output_dir)
    except ManifestError as exc:
        print("输入校验失败：", file=sys.stderr)
        if exc.source is not None:
            print(f"  文件：{exc.source}", file=sys.stderr)
        for message in exc.messages:
            print(f"  - {message}", file=sys.stderr)
        print("未写入任何输出文件。", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"输出失败：{exc}", file=sys.stderr)
        return 3

    summary = json.loads(Path(outcome.report_json).read_text(encoding="utf-8"))["summary"]
    if not args.quiet:
        print("字幕核对完成")
        print(f"  清单：{outcome.manifest_path}")
        print(f"  帧图目录：{outcome.frames_dir}")
        print(f"  输出目录：{outcome.output_dir.resolve()}")
        print(f"  总条目数 {summary['total']}｜通过 {summary['pass']}｜越界 {summary['overflow']}｜"
              f"遮挡 {summary['occlusion']}｜未核对 {summary['unverified']}｜时间异常 {summary['timing']}｜"
              f"重叠对 {summary['overlap_pairs']}")
        print(f"  报告：{outcome.report_json.name}、{outcome.report_md.name}；预览图 {outcome.preview_count} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
