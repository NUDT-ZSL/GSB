#!/usr/bin/env python3
"""确定性生成命中拾取样例场景 JSON。

场景设计覆盖任务全部场景：三类图元、两层嵌套分组（平移/缩放/旋转）、
重叠图元、凹多边形、边界点击、负缩放镜像、缩放为 0 的退化分组、
未知类型与缺字段的非法条目、以及一条非法点击。

运行：python -I tools/generate_sample.py samples/scene_sample.json
重复运行生成字节级一致的文件（内容全部为常量，无随机数、无时间戳）。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

# Windows 控制台默认编码可能是 GBK，统一改为 UTF-8，避免中文提示乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# 方形半边长 50、旋转 45 度后的角点半径 = 50 * sqrt(2)
CORNER_RADIUS = 50.0 * math.sqrt(2.0)
# 两层嵌套组（外层旋转 45 度、内层缩放 2 倍）内方形角点的世界坐标半径
NESTED_CORNER_RADIUS = 30.0 * math.sqrt(2.0) * 2.0


def build_scene() -> dict:
    nodes = [
        # 1) 普通矩形：局部 (0,0)-(120,80)，平移到 (20,20)
        {
            "id": "rect-base",
            "type": "rect",
            "shape": {"x": 0.0, "y": 0.0, "width": 120.0, "height": 80.0},
            "transform": {"translate": [20.0, 20.0]},
        },
        # 2) 缩放 2 倍组内矩形：局部中心 (25,15) -> 世界 (150,110)
        #    点击世界 (150,110) 回映局部应得 (25,15)
        {
            "id": "group-scale2",
            "type": "group",
            "transform": {"translate": [100.0, 80.0], "scale": 2.0},
            "children": [
                {
                    "id": "rect-scaled",
                    "type": "rect",
                    "shape": {"x": 0.0, "y": 0.0, "width": 50.0, "height": 30.0},
                }
            ],
        },
        # 3) 旋转 45 度组内方形（边长 100，中心在世界 (350,90)）
        #    方形角点旋转后落在中心正上方，半径 50*sqrt(2)
        {
            "id": "group-rotate45",
            "type": "group",
            "transform": {"translate": [350.0, 90.0], "rotate": 45.0},
            "children": [
                {
                    "id": "square-rotated",
                    "type": "rect",
                    "shape": {"x": -50.0, "y": -50.0, "width": 100.0, "height": 100.0},
                }
            ],
        },
        # 4) 两层嵌套组：外层旋转 45 度 + 平移，内层缩放 2 倍
        #    方形角点局部 (30,30)，世界半径 = 30*sqrt(2)*2，位于中心正上方
        {
            "id": "group-nested-outer",
            "type": "group",
            "transform": {"translate": [520.0, 120.0], "rotate": 45.0},
            "children": [
                {
                    "id": "group-nested-inner",
                    "type": "group",
                    "transform": {"scale": [2.0, 2.0]},
                    "children": [
                        {
                            "id": "rect-nested",
                            "type": "rect",
                            "shape": {"x": -30.0, "y": -30.0, "width": 60.0, "height": 60.0},
                        }
                    ],
                }
            ],
        },
        # 5) 普通圆：圆心 (120,250)，半径 40
        {
            "id": "circle-base",
            "type": "circle",
            "shape": {"cx": 0.0, "cy": 0.0, "radius": 40.0},
            "transform": {"translate": [120.0, 250.0]},
        },
        # 6) L 形凹多边形（局部坐标），平移到 (260,200)
        #    60x60 方框挖掉右上角 30x30：凹口为 x∈(30,60), y∈(0,30)
        #    局部 (15,15) 在实体上；局部 (45,15) 在凹口空洞内
        {
            "id": "group-lshape",
            "type": "group",
            "transform": {"translate": [260.0, 200.0]},
            "children": [
                {
                    "id": "polygon-lshape",
                    "type": "polygon",
                    "shape": {
                        "points": [
                            [0.0, 0.0],
                            [30.0, 0.0],
                            [30.0, 30.0],
                            [60.0, 30.0],
                            [60.0, 60.0],
                            [0.0, 60.0],
                        ]
                    },
                }
            ],
        },
        # 7) 重叠矩形：二者在 (460,210)-(480,230) 重叠，后者后绘制（更上层）
        {
            "id": "rect-under",
            "type": "rect",
            "shape": {"x": 420.0, "y": 190.0, "width": 60.0, "height": 40.0},
        },
        {
            "id": "rect-over",
            "type": "rect",
            "shape": {"x": 460.0, "y": 210.0, "width": 60.0, "height": 40.0},
        },
        # 8) 负缩放镜像组：x 方向 scale=-1，矩形关于世界 x=650 左右镜像
        #    局部矩形 x∈[-30,30] 镜像后覆盖世界 x∈[620,680]；
        #    世界 (640,260) -> 局部 (10,10)（命中），对称点 (660,260) -> 局部 (-10,10)（同样命中，左右对称）；
        #    世界 (610,260) 与 (690,260) 为镜像外侧的一对对称点，均不命中
        {
            "id": "group-mirror",
            "type": "group",
            "transform": {"translate": [650.0, 250.0], "scale": [-1.0, 1.0]},
            "children": [
                {
                    "id": "rect-mirrored",
                    "type": "rect",
                    "shape": {"x": -30.0, "y": -10.0, "width": 60.0, "height": 20.0},
                }
            ],
        },
        # 9) 缩放为 0 的退化分组：矩阵不可逆，子树标注不可用并跳过拾取
        {
            "id": "group-zero-scale",
            "type": "group",
            "transform": {"translate": [200.0, 380.0], "scale": 0.0},
            "children": [
                {
                    "id": "rect-in-zero-group",
                    "type": "rect",
                    "shape": {"x": -20.0, "y": -10.0, "width": 40.0, "height": 20.0},
                }
            ],
        },
        # 10) 非法条目：未知图元类型
        {
            "id": "shape-unknown",
            "type": "ellipse",
            "shape": {"cx": 0.0, "cy": 0.0, "rx": 5.0, "ry": 3.0},
        },
        # 11) 非法条目：矩形缺少 width
        {
            "id": "rect-broken",
            "type": "rect",
            "shape": {"x": 0.0, "y": 0.0, "height": 10.0},
        },
    ]

    clicks = [
        # (1)/(3) 缩放组内矩形中心：回映局部 (25,15)
        {"x": 150.0, "y": 110.0, "note": "缩放2倍组内矩形中心"},
        # (2) 重叠区域：应返回后绘制的 rect-over
        {"x": 470.0, "y": 220.0, "note": "两矩形重叠区域，取最上层"},
        # (4) 空白区域：未命中
        {"x": 30.0, "y": 400.0, "note": "空白区域"},
        # (5) 旋转 45 度方形角点：回映局部应接近 (-50,-50)
        {"x": 350.0, "y": 90.0 - CORNER_RADIUS, "note": "旋转方形角点"},
        # (6) 两层嵌套组内方形角点：回映局部应接近 (30,30)
        {"x": 520.0, "y": 120.0 - NESTED_CORNER_RADIUS, "note": "嵌套组内方形角点"},
        # (6) L 形凹口外（实体上）：命中，局部 (15,15)
        {"x": 275.0, "y": 215.0, "note": "L形凹多边形实体处"},
        # (6) L 形凹口内（空洞处）：未命中
        {"x": 305.0, "y": 215.0, "note": "L形凹多边形凹口内"},
        # (7) 矩形右边线：边界命中，局部 x=120
        {"x": 140.0, "y": 60.0, "note": "rect-base 右边线"},
        # (7) 圆周边：边界命中，局部到圆心距离恰为半径
        {"x": 160.0, "y": 250.0, "note": "circle-base 圆周边"},
        # (7) 多边形顶点：边界命中（局部顶点 (30,30) 的世界位置）
        {"x": 290.0, "y": 230.0, "note": "L形多边形顶点"},
        # (8) 镜像组内关于 x=650 左右对称的两点：均命中，回映局部坐标互为镜像 (10,10)/(-10,10)
        {"x": 640.0, "y": 260.0, "note": "镜像组内（对称轴左侧，命中）"},
        {"x": 660.0, "y": 260.0, "note": "镜像组内（对称轴右侧，命中，与上一点左右对称）"},
        # (8) 镜像外侧的一对对称点：均不命中
        {"x": 610.0, "y": 260.0, "note": "镜像组外左侧（未命中）"},
        {"x": 690.0, "y": 260.0, "note": "镜像组外右侧（未命中，与上一点左右对称）"},
        # (8) 退化分组坍缩点：不可用，未命中
        {"x": 200.0, "y": 380.0, "note": "缩放为0分组的坍缩点"},
        # (10) 非法点击条目：缺少 y
        {"x": 500.0, "note": "非法点击（缺少y）"},
    ]

    return {
        "scene": {"id": "sample-scene-001", "version": 1},
        "nodes": nodes,
        "clicks": clicks,
    }


def main(argv: list | None = None) -> int:
    output = Path(argv[0]) if argv else Path("samples/scene_sample.json")
    if output.parent and not output.parent.exists():
        output.parent.mkdir(parents=True, exist_ok=True)
    scene = build_scene()
    with output.open("w", encoding="utf-8") as handle:
        json.dump(scene, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"样例场景已生成: {output}（图元/分组节点 {len(scene['nodes'])} 个，点击 {len(scene['clicks'])} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
