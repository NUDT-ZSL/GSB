# -*- coding: utf-8 -*-
"""
make_samples.py —— 确定性生成验收用 JSON 样本（任务代码角色）。

所有数据为固定字面量，不使用随机源，保证每次运行逐字节一致。
输出目录：samples/
"""

import json
import os

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")

SAMPLES = {
    # 基础场景：红矩形 + 蓝圆形 + 绿色线段（含描边矩形，验证 line_width 透传）
    "basic.json": {
        "width": 200,
        "height": 120,
        "background": "#ffffff",
        "primitives": [
            {"type": "rect", "x": 10, "y": 10, "width": 80, "height": 60, "color": "#ff0000"},
            {"type": "circle", "cx": 140, "cy": 60, "radius": 35, "color": "#0000ff"},
            {"type": "line", "x1": 0, "y1": 110, "x2": 200, "y2": 110,
             "color": "#00aa44", "line_width": 4},
            {"type": "rect", "x": 120, "y": 10, "width": 50, "height": 30,
             "color": "#000000", "line_width": 2},
        ],
    },

    # alpha 合成：红矩形上叠加 50% 透明度蓝矩形，期望混合区约 (127, 0, 128)
    "alpha.json": {
        "width": 100,
        "height": 100,
        "background": "#ffffff",
        "primitives": [
            {"type": "rect", "x": 10, "y": 10, "width": 80, "height": 80, "color": "#ff0000"},
            {"type": "rect", "x": 20, "y": 20, "width": 60, "height": 60,
             "color": "#0000ff", "opacity": 0.5},
        ],
    },

    # 跳过场景：合法圆形 + 未知 triangle + 负半径圆 + 错误颜色 + 缺字段矩形
    "skip.json": {
        "width": 160,
        "height": 160,
        "background": "#f5f5f5",
        "primitives": [
            {"type": "circle", "cx": 80, "cy": 80, "radius": 50, "color": "#3366cc"},
            {"type": "triangle", "points": [0, 0, 10, 10, 0, 10], "color": "#000000"},
            {"type": "circle", "cx": 20, "cy": 20, "radius": -5, "color": "#000000"},
            {"type": "rect", "x": 0, "y": 0, "width": 10, "height": 10, "color": "red"},
            {"type": "rect", "x": 0, "y": 0, "width": 10, "color": "#000000"},
        ],
    },

    # 缺 width 字段（验收 1）
    "missing_width.json": {
        "height": 100,
        "background": "#ffffff",
        "primitives": [],
    },

    # width 越界 5000（验收 8）
    "too_wide.json": {
        "width": 5000,
        "height": 100,
        "background": "#ffffff",
        "primitives": [],
    },

    # height 非法（零/负数边界）
    "bad_height.json": {
        "width": 100,
        "height": 0,
        "background": "#ffffff",
        "primitives": [],
    },

    # 背景色格式错误
    "bad_background.json": {
        "width": 100,
        "height": 100,
        "background": "white",
        "primitives": [],
    },

    # 内容不是合法 JSON（验收 9 第二项）
    "broken.json": "{ this is : not valid json, ,, }",
}


def main():
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    for name, payload in SAMPLES.items():
        path = os.path.join(SAMPLE_DIR, name)
        if isinstance(payload, str):
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
        else:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
                fh.write("\n")
    print("已生成 %d 个确定性样本到 %s" % (len(SAMPLES), SAMPLE_DIR))
    for name in sorted(SAMPLES):
        print("  -", name)


if __name__ == "__main__":
    main()
