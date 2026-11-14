"""命令行入口：python -m srt_safety --srt ... --config ... --out ..."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .audit import run_audit
from .config import ConfigError, load_config
from .fonts import load_font
from .parser import parse_srt
from .render import render_frame
from .report import write_reports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m srt_safety",
        description="读取 SRT 字幕与 JSON 画幅配置，输出帧区间、安全区边距、"
                    "冲突清单与带安全区框的代表帧 PNG（纯本地，无网络/媒体解码）。",
    )
    parser.add_argument("--srt", required=True, help="SRT 字幕文件路径")
    parser.add_argument("--config", required=True, help="JSON 画幅配置路径")
    parser.add_argument("--out", required=True, help="输出目录（不存在则创建）")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK 时保证中文输出不崩
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)

    # 配置非法：可读错误 + 退出码 2
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    try:
        cues, parse_errors = parse_srt(args.srt)
    except FileNotFoundError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2

    font = load_font(cfg.font_size)
    audit = run_audit(cues, parse_errors, cfg, font)
    records = audit["records"]

    # 补零位数 = 最大序号的十进制位数 + 1：
    # 题设核对样例要求最大序号 12 时文件名为 001.png ... 012.png（3 位）；
    # 无有效字幕时退化为 2 位，保证至少能容纳两位序号
    max_index = max((r.index for r in records), default=0)
    padding = len(str(max_index)) + 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    safe_box = {
        "x0": cfg.safe_x,
        "y0": cfg.safe_y,
        "x1": cfg.width - cfg.safe_x,
        "y1": cfg.height - cfg.safe_y,
        "width": cfg.safe_w,
        "height": cfg.safe_h,
    }

    write_reports(
        out_dir, cfg, safe_box, records, parse_errors,
        audit["overlap_pairs"], audit["inverted_indexes"],
        audit["total_blocks"], audit["anomalous_count"], padding,
        args.srt, args.config,
    )

    # 代表帧：每条有效字幕一张，同屏可见的交叠字幕一并错位绘制
    rendered = 0
    for target in records:
        visible = [
            other
            for other in records
            if not other.inverted
            and other.start_frame < target.end_frame
            and other.end_frame > target.start_frame
        ]
        if target.inverted:
            visible = [target]
        out_png = f"{target.index:0{padding}d}.png"
        render_frame(cfg, font, target, visible, out_dir / out_png)
        rendered += 1

    print("字幕安全区审计完成")
    print(f"  SRT 总块数      : {audit['total_blocks']}")
    print(f"  有效字幕条数    : {len(records)}（已渲染 {rendered} 张代表帧）")
    print(f"  坏块跳过        : {len(parse_errors)}")
    print(f"  帧倒置          : {len(audit['inverted_indexes'])}")
    print(f"  帧区间交叠对    : {len(audit['overlap_pairs'])}")
    print(f"  越出安全区      : {sum(1 for r in records if r.outside_safe)}")
    print(f"  异常记录数      : {audit['anomalous_count']}")
    print(f"  输出目录        : {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
