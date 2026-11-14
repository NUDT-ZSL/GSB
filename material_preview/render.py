"""逐方向的光照预览渲染。

设计要点（对应验收规则 #6 与 #5）：

* 同一材质的每个方向都接收**同一份** base/normal 数组，渲染是纯函数，
  不修改输入 —— 方向之间只有 light_dir 不同。
* 零向量法线像素统一输出清单约定的中性色（默认 (128,128,255)），
  不参与 Lambert 计算，也不会出现 NaN 污染整图。
* 受光结果只由 N·L 决定；环境光项与贴图无关的乘数对所有方向一致，
  因此未受光区域的颜色完全来自基础色贴图，便于肉眼对照。
"""

from __future__ import annotations

import math

import numpy as np

from .textures import BaseColorMap, NormalMap

# 环境光比例：保证背面不至于全黑，同时让受光差异足够明显。
AMBIENT = 0.25

# 水平光照方向：0° 指向 +X（画面右方），90° 指向 +Y（画面上方）。
# light vector 指向「从表面看向光源」的方向，与法线点乘即 Lambert 项。
def light_vector(angle_degrees: float) -> np.ndarray:
    radians = math.radians(float(angle_degrees))
    return np.array(
        [math.cos(radians), math.sin(radians), 0.0], dtype=np.float64
    )


def render_direction(
    base: BaseColorMap,
    normal: NormalMap,
    angle_degrees: float,
    neutral_color: tuple[int, int, int],
) -> np.ndarray:
    """渲染单个光照方向的预览，返回 (H, W, 3) uint8 数组。"""
    light_dir = light_vector(angle_degrees)
    lambert = (normal.vectors * light_dir).sum(axis=2)
    intensity = AMBIENT + (1.0 - AMBIENT) * np.clip(lambert, 0.0, 1.0)

    shaded = np.clip(base.pixels * intensity[..., None], 0.0, 1.0)
    shaded = np.rint(shaded * 255.0).astype(np.uint8)

    # 零向量像素不参与光照，直接落入约定中性色。
    shaded[normal.zero_mask] = np.array(neutral_color, dtype=np.uint8)
    return shaded
