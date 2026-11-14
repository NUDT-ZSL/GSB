"""图元自身坐标系下的点包含判定。

所有判定均在图元的局部坐标系中进行（调用方负责把屏幕点逐层回映）。
边界（矩形边线、圆周边、多边形边与顶点）按命中处理，使用统一容差
BOUNDARY_EPS 吸收浮点误差，避免"差几个像素"的边界漏检。
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

# 边界判定容差（局部坐标单位）。拾取回映到局部坐标后使用，
# 因此该容差与图元定义处于同一尺度，不受外层缩放影响。
BOUNDARY_EPS = 1e-8

Point = Tuple[float, float]


def _to_point(value: Sequence[float]) -> Point:
    return float(value[0]), float(value[1])


def hit_rect(point: Sequence[float], shape: dict) -> bool:
    """矩形包含判定，shape: {x, y, width, height}，含四条边线。"""
    px, py = _to_point(point)
    x = float(shape["x"])
    y = float(shape["y"])
    width = float(shape["width"])
    height = float(shape["height"])

    x_min, x_max = min(x, x + width), max(x, x + width)
    y_min, y_max = min(y, y + height), max(y, y + height)
    eps = BOUNDARY_EPS
    return (x_min - eps <= px <= x_max + eps
            and y_min - eps <= py <= y_max + eps)


def hit_circle(point: Sequence[float], shape: dict) -> bool:
    """圆包含判定，shape: {cx, cy, radius}，含圆周边。"""
    px, py = _to_point(point)
    cx = float(shape["cx"])
    cy = float(shape["cy"])
    radius = float(shape["radius"])
    distance = math.hypot(px - cx, py - cy)
    return distance <= radius + BOUNDARY_EPS


def _point_on_segment(px: float, py: float,
                      p1: Point, p2: Point) -> bool:
    """判断点是否落在线段 p1-p2 上（含两个端点）。"""
    x1, y1 = p1
    x2, y2 = p2
    cross = (px - x1) * (y2 - y1) - (py - y1) * (x2 - x1)
    if abs(cross) > BOUNDARY_EPS:
        return False
    # 共线后再用包围盒确认点在线段范围内
    return (min(x1, x2) - BOUNDARY_EPS <= px <= max(x1, x2) + BOUNDARY_EPS
            and min(y1, y2) - BOUNDARY_EPS <= py <= max(y1, y2) + BOUNDARY_EPS)


def hit_polygon(point: Sequence[float], shape: dict) -> bool:
    """多边形包含判定，shape: {points: [[x,y], ...]}。

    采用射线法，支持凹多边形；先做边/顶点的边界判定，
    因此点击恰好落在边线或顶点上时也判为命中。
    """
    px, py = _to_point(point)
    vertices: List[Point] = [
        (float(v[0]), float(v[1])) for v in shape["points"]
    ]
    if len(vertices) < 3:
        raise ValueError("多边形至少需要 3 个顶点")

    # 1) 边界与顶点判定（闭合多边形，首尾自动相连）
    for i in range(len(vertices)):
        if _point_on_segment(px, py, vertices[i],
                             vertices[(i + 1) % len(vertices)]):
            return True

    # 2) 射线法判定内部，凹多边形同样适用
    inside = False
    n = len(vertices)
    for i in range(n):
        x1, y1 = vertices[i]
        x2, y2 = vertices[(i + 1) % n]
        if (y1 > py) != (y2 > py):
            x_cross = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            if px < x_cross:
                inside = not inside
    return inside


HIT_TESTERS = {
    "rect": hit_rect,
    "circle": hit_circle,
    "polygon": hit_polygon,
}
