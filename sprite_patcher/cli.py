"""命令行入口。

用法::

    python -m sprite_patcher --config CONFIG --sheet SHEET --out-sheet OUT_PNG
                             --out-report OUT_JSON --out-preview OUT_HTML
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

from .config import load_config
from .errors import ConfigError
from .geometry import build_geometry
from .patcher import apply_patches
from .preview import build_preview_html
from .report import build_report, diff_cells

USAGE = (
    "用法: python -m sprite_patcher --config <配置JSON> --sheet <原精灵表PNG> "
    "--out-sheet <新精灵表PNG> --out-report <差异报告JSON> "
    "--out-preview <预览HTML>\n"
    "五个参数均为必填，缺失时打印本用法并以退出码 2 结束。"
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sprite_patcher",
        description="按格子坐标将补丁图叠加到精灵表，输出新表、差异报告与对比预览。",
        add_help=True,
    )
    parser.add_argument("--config", required=True, help="网格与补丁清单配置 JSON")
    parser.add_argument("--sheet", required=True, help="现有精灵表 PNG 路径")
    parser.add_argument("--out-sheet", required=True, help="输出新精灵表 PNG 路径")
    parser.add_argument("--out-report", required=True, help="输出差异报告 JSON 路径")
    parser.add_argument("--out-preview", required=True, help="输出对比预览 HTML 路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    # argparse 在缺参/缺值时自动打印用法并以退出码 2 退出
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        print("未生成任何输出文件。", file=sys.stderr)
        return 1

    # 底图尺寸必须与配置一致，否则格线坐标没有意义
    try:
        base_img = Image.open(args.sheet)
        base_img.load()
    except (OSError, FileNotFoundError) as exc:
        print(f"[错误] 无法读取原精灵表 {args.sheet}: {exc}", file=sys.stderr)
        return 1
    if (base_img.width, base_img.height) != (config.sheet.width, config.sheet.height):
        print(
            f"[错误] 精灵表实际尺寸 {base_img.width}x{base_img.height} "
            f"与配置 /sheet 的 {config.sheet.width}x{config.sheet.height} 不一致。",
            file=sys.stderr,
        )
        return 1
    base_img = base_img.convert("RGBA")

    try:
        geometry = build_geometry(config.sheet, config.grid)
    except ValueError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    new_img, outcomes, cell_stats = apply_patches(base_img, config, geometry)
    diffs = diff_cells(base_img, new_img, config, geometry)
    report = build_report(
        config, geometry, outcomes, cell_stats, diffs,
        input_sheet=args.sheet,
        output_sheet=args.out_sheet,
    )

    for out in (args.out_sheet, args.out_report, args.out_preview):
        Path(out).parent.mkdir(parents=True, exist_ok=True)

    new_img.save(args.out_sheet, format="PNG")
    Path(args.out_report).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    build_preview_html(
        base_img, new_img, config, geometry, diffs, report, args.out_preview
    )

    s = report["summary"]
    print(
        f"完成：补丁 {s['patch_count']} 个（有效 {s['applied_patch_count']}，"
        f"不可用 {s['unavailable_patch_count']}），变化格 {s['changed_cell_count']}，"
        f"变化像素 {s['total_changed_pixels']}"
        + ("（本次无实质修改）" if s["no_substantial_change"] else "")
    )
    print(f"  新精灵表: {args.out_sheet}")
    print(f"  差异报告: {args.out_report}")
    print(f"  预览页:   {args.out_preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
