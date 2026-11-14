# -*- coding: utf-8 -*-
"""生成本地可复现的样例动画 JSON（无需任何外部数据文件）。

运行： python -m tools.generate_samples
输出目录： samples/
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:          # 兼容 python -I 直接执行脚本
    sys.path.insert(0, ROOT)

SAMPLE_DIR = os.path.join(ROOT, "samples")


def _write(name: str, data: dict) -> str:
    path = os.path.join(SAMPLE_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return path


def main() -> None:
    os.makedirs(SAMPLE_DIR, exist_ok=True)

    # 1) basic.json —— 两骨骼各三关键帧；dial 含 0°/350° 最短路径插值验证
    basic = {
        "name": "基础联动样例",
        "duration": 2.0,
        "bones": [
            {
                "name": "root",
                "length": 90,
                "keyframes": [
                    {"time": 0.0, "translation": [0, 0], "rotation": 0},
                    {"time": 1.0, "rotation": 90},                       # 缺平移/缩放 -> 默认值
                    {"time": 2.0, "translation": [40, 0], "rotation": 0,
                     "scale": [1.2, 1.2]},
                ],
            },
            {
                "name": "child",
                "parent": "root",
                "length": 70,
                "keyframes": [
                    {"time": 0.0, "translation": [90, 0], "rotation": 0},
                    {"time": 1.0, "translation": [90, 0], "rotation": -45},
                    {"time": 2.0, "translation": [90, 0], "rotation": 0},
                ],
            },
            {
                # 0° -> 350°：最短路径中间帧必须为 355°（需求 3）
                "name": "dial",
                "length": 50,
                "keyframes": [
                    {"time": 0.0, "translation": [-140, 120], "rotation": 0},
                    {"time": 2.0, "translation": [-140, 120], "rotation": 350},
                ],
            },
        ],
    }
    _write("basic.json", basic)

    # 2) duplicate.json —— arm 含重复时间关键帧；good 骨骼应照常预览（需求 4）
    duplicate = {
        "name": "重复时间样例",
        "duration": 2.0,
        "bones": [
            {
                "name": "arm",
                "length": 80,
                "keyframes": [
                    {"time": 0.0, "rotation": 0},
                    {"time": 1.0, "rotation": 45},
                    {"time": 1.0, "rotation": 60},   # 与上一条时间重复
                    {"time": 2.0, "rotation": 90},
                ],
            },
            {
                "name": "good",
                "length": 60,
                "keyframes": [
                    {"time": 0.0, "translation": [120, 0], "rotation": 10},
                    {"time": 2.0, "translation": [120, 0], "rotation": 50},
                ],
            },
        ],
    }
    _write("duplicate.json", duplicate)

    # 3) bad_time.json —— 负时间 / 倒序 / 超总时长（需求 6）
    bad_time = {
        "name": "异常时间样例",
        "duration": 2.0,
        "bones": [
            {
                "name": "bad_leg",
                "length": 80,
                "keyframes": [
                    {"time": -0.5, "rotation": -30},   # 负时间，跳过
                    {"time": 0.0, "rotation": 0},
                    {"time": 1.0, "rotation": 45},
                    {"time": 0.5, "rotation": 20},     # 倒序，跳过
                    {"time": 2.5, "rotation": 90},     # 超总时长，跳过
                    {"time": 2.0, "rotation": 60},
                ],
            },
            {
                "name": "normal_leg",
                "length": 70,
                "keyframes": [
                    {"time": 0.0, "translation": [-120, 0], "rotation": 0},
                    {"time": 2.0, "translation": [-120, 0], "rotation": 80},
                ],
            },
        ],
    }
    _write("bad_time.json", bad_time)

    # 4) malformed_keyframe.json —— 单条关键帧格式错误，同骨骼其余帧仍可用
    malformed = {
        "name": "关键帧格式错误样例",
        "duration": 2.0,
        "bones": [
            {
                "name": "spine",
                "length": 80,
                "keyframes": [
                    {"time": 0.0, "rotation": 0},
                    {"rotation": 30},                  # 缺 time -> 跳过并提示
                    {"time": 1.0, "rotation": "大角度"},  # rotation 非法 -> 回落 0
                    {"time": 2.0, "rotation": 70},
                ],
            }
        ],
    }
    _write("malformed_keyframe.json", malformed)

    # 5) broken.json —— 文件级损坏（JSON 语法错误），走"文件不可用"界面（需求 9）
    broken_path = os.path.join(SAMPLE_DIR, "broken.json")
    with open(broken_path, "w", encoding="utf-8") as fh:
        fh.write('{"name": "损坏文件", "bones": [ { "name": "x", ')

    print("样例已生成到", SAMPLE_DIR)
    for fname in sorted(os.listdir(SAMPLE_DIR)):
        print(" -", fname)


if __name__ == "__main__":
    main()
