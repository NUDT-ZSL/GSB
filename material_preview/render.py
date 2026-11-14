"""离线球体渲染器（纯 Python + Pillow，确定性输出）。

坐标约定
--------
* 画布像素以左上角为原点，x 向右、y 向下；像素 (px, py) 的采样点为其中心
  (px + 0.5, py + 0.5)。
* 相机位于 -z 方向无穷远处、沿 +z 做正交投影；从表面指向相机的视线
  方向 V = (0, 0, -1)。球体的 z 越小代表离相机越近，重叠处由深度
  （交点参数 t）决定遮挡，t 相等时按球体在配置中的先后顺序取先出现者，
  保证结果确定。
* 光照方向 L 已在场景校验阶段归一化，方向语义为“从表面指向光源”。

着色模型
--------
* 漫反射：ambient + diffuse * max(0, N·L)
* 高光（仅当材质同时给出高光强度与指数）：Phong 模型
  spec = max(0, R·V) ** exponent * intensity，白色叠加
* 每通道按 floor(c * 255 + 0.5) 做半向上取整并截断到 [0, 255]，
  不依赖平台默认的浮点舍入。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image

from .scene import Material, Scene, Sphere


@dataclass(frozen=True)
class RenderResult:
    """单方向渲染结果。"""

    image: Image.Image
    visible_sphere_count: int


def _quantize_channel(value: float) -> int:
    """把 [0, 1]+ 的线性颜色分量确定性量化为 8 bit 整数。"""
    quantized = math.floor(value * 255.0 + 0.5)
    if quantized < 0:
        return 0
    if quantized > 255:
        return 255
    return quantized


def _shade(material: Material,
           normal: tuple[float, float, float],
           light: tuple[float, float, float]) -> tuple[int, int, int]:
    nx, ny, nz = normal
    lx, ly, lz = light
    ndl = nx * lx + ny * ly + nz * lz
    diffuse_term = ndl if ndl > 0.0 else 0.0

    linear = [
        material.ambient[channel] + material.diffuse[channel] * diffuse_term
        for channel in range(3)
    ]

    if material.has_specular and diffuse_term > 0.0:
        # Phong 反射向量 R = 2(N·L)N - L；视线 V = (0, 0, -1)，故 R·V = -R.z。
        rz = 2.0 * ndl * nz - lz
        rv = -rz
        if rv < 0.0:
            rv = 0.0
        specular = (rv ** material.specular_exponent) * material.specular_intensity
        for channel in range(3):
            linear[channel] += specular  # 白色高光，三通道等量叠加

    return tuple(_quantize_channel(channel) for channel in linear)  # type: ignore[return-value]


def render_direction(scene: Scene,
                     light_direction: tuple[float, float, float]) -> RenderResult:
    """为单个光照方向渲染一张对照图。"""
    width, height = scene.width, scene.height
    image = Image.new("RGB", (width, height), scene.background)

    # 预展开球体数据，避免热循环里反复访问 dataclass 字段。
    sphere_data = tuple(
        (sphere.center[0], sphere.center[1], sphere.center[2],
         sphere.radius, sphere.radius * sphere.radius)
        for sphere in scene.spheres
    )

    visible: set[int] = set()

    for py in range(height):
        ray_y = py + 0.5
        for px in range(width):
            ray_x = px + 0.5

            nearest_t = math.inf
            nearest_index = -1
            nearest_normal: tuple[float, float, float] | None = None

            for index, (cx, cy, cz, radius, radius_sq) in enumerate(sphere_data):
                dx = ray_x - cx
                dy = ray_y - cy
                planar_sq = dx * dx + dy * dy
                if planar_sq > radius_sq:
                    continue  # 射线与球体无交点

                depth_offset = math.sqrt(radius_sq - planar_sq)
                t = cz - depth_offset  # 朝向相机的交点（较小 t）
                # 严格大于：t 相等时保留列表中先出现的球体，顺序确定。
                if t < nearest_t:
                    nearest_t = t
                    nearest_index = index
                    nearest_normal = (dx / radius, dy / radius,
                                      -depth_offset / radius)

            if nearest_normal is not None:
                image.putpixel(
                    (px, py),
                    _shade(scene.material, nearest_normal, light_direction),
                )
                visible.add(nearest_index)

    return RenderResult(image=image, visible_sphere_count=len(visible))


def save_png(image: Image.Image, output_path: str) -> None:
    """确定性保存 PNG：固定压缩级别，不写入修改时间等平台相关元数据。"""
    image.save(output_path, format="PNG", optimize=False, compress_level=6)
