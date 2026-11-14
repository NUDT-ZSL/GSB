"""2D 仿射变换工具。

矩阵采用 3x3 齐次形式，行主序存储为嵌套列表：

    [ a  c  e ]
    [ b  d  f ]
    [ 0  0  1 ]

点 (x, y) 的变换结果为 (a*x + c*y + e, b*x + d*y + f)。

节点的局部变换约定为常见的 SVG/Canvas 组合方式：
先缩放 S，再绕原点旋转 R，最后平移 T，即 local = T * R * S。
嵌套组的世界矩阵按树深自外向内连乘：world = parent_world * local。
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

# 行列式绝对值低于该阈值即判定为退化（不可逆，例如缩放为 0）
SINGULAR_EPS = 1e-12

Matrix = List[List[float]]
Point = Tuple[float, float]


def identity() -> Matrix:
    """返回单位矩阵。"""
    return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def translation(tx: float, ty: float) -> Matrix:
    """平移矩阵。"""
    return [[1.0, 0.0, float(tx)], [0.0, 1.0, float(ty)], [0.0, 0.0, 1.0]]


def scaling(sx: float, sy: float) -> Matrix:
    """缩放矩阵；允许负缩放（镜像）。"""
    return [[float(sx), 0.0, 0.0], [0.0, float(sy), 0.0], [0.0, 0.0, 1.0]]


def rotation(angle_deg: float) -> Matrix:
    """绕原点旋转矩阵，角度为度，正角度表示 x 轴向 y 轴方向旋转。"""
    theta = math.radians(float(angle_deg))
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)
    return [[cos_t, -sin_t, 0.0], [sin_t, cos_t, 0.0], [0.0, 0.0, 1.0]]


def multiply(a: Matrix, b: Matrix) -> Matrix:
    """返回矩阵乘积 a * b（先施加 b 再施加 a）。"""
    return [
        [
            sum(a[i][k] * b[k][j] for k in range(3))
            for j in range(3)
        ]
        for i in range(3)
    ]


def compose(transform_spec: dict) -> Matrix:
    """按场景 JSON 中的 transform 字段组合局部矩阵。

    支持的键（均为可选，缺省按单位值处理）：
    translate: [tx, ty]
    scale:     数值（等比）或 [sx, sy]
    rotate:    角度（度）
    """
    # 注意：局部变量不得与本模块的 translation/scaling/rotation 构造函数同名
    translate_spec = transform_spec.get("translate", [0.0, 0.0])
    scale_spec = transform_spec.get("scale", [1.0, 1.0])
    rotate_spec = transform_spec.get("rotate", 0.0)

    if isinstance(scale_spec, (int, float)):
        sx = sy = float(scale_spec)
    else:
        sx, sy = float(scale_spec[0]), float(scale_spec[1])

    offset_x, offset_y = float(translate_spec[0]), float(translate_spec[1])
    cos_r = math.cos(math.radians(float(rotate_spec)))
    sin_r = math.sin(math.radians(float(rotate_spec)))

    # 直接按解析公式构造 local = T * R * S：
    # 点先缩放 (sx, sy)，再绕原点旋转 rotate，最后平移 (tx, ty)。
    # 显式写出元素，避免矩阵乘法顺序歧义。
    return [
        [cos_r * sx, -sin_r * sy, offset_x],
        [sin_r * sx,  cos_r * sy, offset_y],
        [0.0,         0.0,        1.0],
    ]


def apply(matrix: Matrix, point: Sequence[float]) -> Point:
    """用矩阵变换一个点。"""
    x, y = float(point[0]), float(point[1])
    return (
        matrix[0][0] * x + matrix[0][1] * y + matrix[0][2],
        matrix[1][0] * x + matrix[1][1] * y + matrix[1][2],
    )


def determinant(matrix: Matrix) -> float:
    """计算 3x3 仿射矩阵的行列式（末行固定为 0,0,1）。"""
    a, c, e = matrix[0]
    b, d, f = matrix[1]
    return a * d - b * c


def inverse(matrix: Matrix) -> Matrix:
    """求逆矩阵；行列式接近 0 时抛出 ValueError。"""
    a, c, e = matrix[0]
    b, d, f = matrix[1]
    det = a * d - b * c
    if abs(det) < SINGULAR_EPS:
        raise ValueError("变换矩阵不可逆（疑似缩放为 0 的退化分组）")
    inv_det = 1.0 / det
    return [
        [d * inv_det, -c * inv_det, (c * f - d * e) * inv_det],
        [-b * inv_det, a * inv_det, (b * e - a * f) * inv_det],
        [0.0, 0.0, 1.0],
    ]


def is_singular(matrix: Matrix) -> bool:
    """判断矩阵是否退化（不可逆）。"""
    return abs(determinant(matrix)) < SINGULAR_EPS
