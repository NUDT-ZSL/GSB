#!/usr/bin/env python3
"""确定性生成业务样例：场景背景 PNG + 标签清单 JSON。

固定随机种子，多次运行产物字节一致。

用法:
    python gen_sample.py --outdir samples
"""
from __future__ import annotations

import argparse
import json
import os
import random

from PIL import Image, ImageDraw

WIDTH, HEIGHT = 1600, 900
SAFE_RIGHT = 1280  # 安全区右边界（小于背景宽，用于演示溢出）


def make_background(path: str):
    """渐变天空 + 确定性散布的山丘/星星，模拟游戏场景底图。"""
    img = Image.new("RGB", (WIDTH, HEIGHT))
    px = img.load()
    for yy in range(HEIGHT):
        t = yy / (HEIGHT - 1)
        r = int(24 + 40 * t)
        g = int(32 + 60 * t)
        b = int(72 + 90 * t)
        for xx in range(WIDTH):
            px[xx, yy] = (r, g, b)
    rng = random.Random(88)  # 固定种子 -> 确定性
    draw = ImageDraw.Draw(img)
    for _ in range(120):  # 星星
        x = rng.randrange(WIDTH)
        y = rng.randrange(HEIGHT // 2)
        c = rng.randrange(180, 256)
        draw.ellipse([x, y, x + 2, y + 2], fill=(c, c, c))
    for _ in range(8):  # 远山
        cx = rng.randrange(WIDTH)
        cy = rng.randrange(HEIGHT // 2, HEIGHT)
        rw = rng.randrange(200, 500)
        rh = rng.randrange(80, 200)
        draw.ellipse([cx - rw, cy - rh, cx + rw, cy + rh],
                     fill=(30, 58, 95))
    img.save(path, format="PNG")


def make_manifest(path: str):
    long_text = "游荡的精英怪物·暮色森林的远古守护者·Lv.99"
    manifest = {
        "background": {"width": WIDTH, "height": HEIGHT},
        "style": {
            "font_path": r"C:\Windows\Fonts\msyh.ttc",
            "fill": "#FFFFFF",
            "outline": {"width": 2, "color": "#1A1A1A"},
            "shadow": {"offset": [2, 2], "color": "#000000A0"},
        },
        "safe_area": {"left": 0, "top": 0, "right": SAFE_RIGHT, "bottom": HEIGHT},
        "labels": [
            # 常规标签
            {"id": "title", "text": "暮色森林", "x": 100.4, "y": 60, "font_size": 48},
            # 小数坐标四舍五入对照：100.6 -> 101
            {"id": "subtitle", "text": "第三章 · 低语之径", "x": 100.6, "y": 140, "font_size": 28},
            {"id": "npc-blacksmith", "text": "铁匠·奥尔加", "x": 640, "y": 320, "font_size": 30},
            {"id": "hp-bar-label", "text": "HP 860/860", "x": 645.5, "y": 360, "font_size": 22},
            # 越过安全区右边界（1280）的标签
            {"id": "overflow-right", "text": "限时活动:双倍经验", "x": 1100, "y": 200, "font_size": 30},
            # 完全不可见：落点 (-50,-50)，短文字整体在画布外
            {"id": "offscreen", "text": "隐", "x": -50, "y": -50, "font_size": 20},
            # 部分被左边缘裁剪
            {"id": "clipped-left", "text": "部分被裁剪的路牌", "x": -30, "y": 500, "font_size": 26},
            # 空文字 -> 跳过
            {"id": "empty-text", "text": "", "x": 300, "y": 700, "font_size": 24},
            # 字号 < 1 -> 跳过
            {"id": "zero-size", "text": "看不见我", "x": 300, "y": 740, "font_size": 0},
            # 超长文字 -> 越过安全区并被右边缘裁剪
            {"id": "long-name", "text": long_text, "x": 900, "y": 820, "font_size": 34},
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main():
    ap = argparse.ArgumentParser(description="确定性生成样例背景与清单")
    ap.add_argument("--outdir", default="samples", help="样例输出目录")
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    bg_path = os.path.join(args.outdir, "background.png")
    mf_path = os.path.join(args.outdir, "manifest.json")
    make_background(bg_path)
    make_manifest(mf_path)
    print(f"已生成: {bg_path}")
    print(f"已生成: {mf_path}")


if __name__ == "__main__":
    main()
