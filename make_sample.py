#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_sample.py — 确定性生成演示用输入 sample_character.json。

场景：一条两节“机械臂”矩形蒙皮网格（root→mid→tip 三层骨骼），
root 摆动 0°→45°→0°，mid 反向弯曲，输出 8 帧 128x128 PNG。
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# 网格：沿 x 方向的两段矩形条，4 列 x 2 行共 8 顶点，6 个三角形
verts = [
    [20, 54], [20, 74],    # 0,1  根部
    [50, 54], [50, 74],    # 2,3  第一节末
    [80, 54], [80, 74],    # 4,5  第二节末
    [110, 54], [110, 74],  # 6,7  末端
]
tris = [
    [0, 2, 1], [1, 2, 3],
    [2, 4, 3], [3, 4, 5],
    [4, 6, 5], [5, 6, 7],
]
# 骨骼：root 原点在 (20,64)，mid 在 (50,64)，tip 在 (80,64)（rest 局部平移逐级累积）
bones = [
    {"name": "root", "parent": None, "rest": {"translate": [20, 64], "rotate": 0}},
    {"name": "mid", "parent": "root", "rest": {"translate": [30, 0], "rotate": 0}},
    {"name": "tip", "parent": "mid", "rest": {"translate": [30, 0], "rotate": 0}},
]
# 权重：根部顶点全绑 root，末端全绑 tip，关节附近线性过渡
weights = [
    [[0, 1.0]], [[0, 1.0]],
    [[0, 0.5], [1, 0.5]], [[0, 0.5], [1, 0.5]],
    [[1, 0.5], [2, 0.5]], [[1, 0.5], [2, 0.5]],
    [[2, 1.0]], [[2, 1.0]],
]
keyframes = [
    {"time": 0.0, "bones": {"root": {"translate": [20, 64], "rotate": 0},
                            "mid": {"translate": [30, 0], "rotate": 0},
                            "tip": {"translate": [30, 0], "rotate": 0}}},
    {"time": 2.0, "bones": {"root": {"translate": [20, 64], "rotate": 45},
                            "mid": {"translate": [30, 0], "rotate": -30},
                            "tip": {"translate": [30, 0], "rotate": 15}}},
    {"time": 4.0, "bones": {"root": {"translate": [20, 64], "rotate": 0},
                            "mid": {"translate": [30, 0], "rotate": 0},
                            "tip": {"translate": [30, 0], "rotate": 0}}},
]

scene = {
    "mesh": {"vertices": verts, "triangles": tris, "weights": weights},
    "bones": bones,
    "animation": {"keyframes": keyframes},
    "output": {"width": 128, "height": 128, "frames": 8,
               "start_time": 0.0, "end_time": 4.0,
               "color": [80, 160, 255, 255],
               "directory": "frames"},
}

path = os.path.join(HERE, "sample_character.json")
with open(path, "w", encoding="utf-8") as f:
    json.dump(scene, f, ensure_ascii=False, indent=2)
print(f"已生成: {path}")
