"""材质通道映射。

所有映射都在整幅全局坐标 (x0,y0) 下进行，归一化常量仅由材质参数决定，
因此分块调用与整幅调用在同一像素上得到完全一致的结果。
"""

from __future__ import annotations

import numpy as np

from .model import MaterialSpec
from .noise import fbm_field

# 单倍频 Perlin 理论幅度约 1，振幅加权和已在 fbm_field 中归一化，
# 这里采用固定扰动系数，常量不随分块尺寸/边界变化。
COLOR_PERTURBATION = 0.15


def build_channels(
    spec: MaterialSpec,
    width: int | None = None,
    height: int | None = None,
    x0: int = 0,
    y0: int = 0,
    field: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    """生成 color/roughness/normal（及 height）通道。

    width/height 用于分块；默认整幅。field 可注入预生成噪声以便复用。
    """
    width = spec.width if width is None else width
    height = spec.height if height is None else height
    if field is None:
        field = fbm_field(width, height, spec.seed, spec.frequencies, spec.amplitudes, x0, y0)

    # 粗糙度：基准粗糙度叠加噪声扰动，统一裁剪到 [0,1]。
    roughness = np.clip(spec.roughness + 0.25 * field, 0.0, 1.0)

    # 颜色：基础色按同一噪声场做各通道固定系数扰动，相位与整幅一致。
    base = np.array(spec.base_color, dtype=np.float64)
    perturb = COLOR_PERTURBATION * field[..., None]
    color = np.clip(base[None, None, :] + perturb * np.array([1.0, 0.8, 0.6]), 0.0, 1.0)

    # 法线：高度场有限差分 -> 切线空间法线，逐像素归一化。
    # 邻居按整幅全局坐标周期环绕定位。若邻居落在当前窗口之外（分块边界
    # 1~2 像素），按全局坐标单独补算该噪声点，保证分块与整幅同口径。
    global_xs = np.arange(x0, x0 + width)
    global_ys = np.arange(y0, y0 + height)
    gx, gy = np.meshgrid(global_xs, global_ys)

    def neighbor_field(dx: int, dy: int) -> np.ndarray:
        nx = (gx + dx) % spec.width
        ny = (gy + dy) % spec.height
        result = np.empty_like(field)
        local_nx = nx - x0
        local_ny = ny - y0
        inside = (local_nx >= 0) & (local_nx < width) & (local_ny >= 0) & (local_ny < height)
        result[inside] = field[local_ny[inside], local_nx[inside]]
        if (~inside).any():
            outside = ~inside
            from .noise import _perlin

            # 窗口外的环绕邻居数量很少，一次性向量化补算，仍走同一纯函数噪声。
            xs = nx[outside].astype(np.float64)
            ys = ny[outside].astype(np.float64)
            total = np.zeros_like(xs, dtype=np.float64)
            weight_sum = 0.0
            for frequency, amplitude in zip(spec.frequencies, spec.amplitudes):
                total += float(amplitude) * _perlin(xs * float(frequency), ys * float(frequency), spec.seed)
                weight_sum += float(amplitude)
            result[np.nonzero(outside)[0], np.nonzero(outside)[1]] = total / weight_sum
        return result

    dh_dx = (neighbor_field(1, 0) - neighbor_field(-1, 0)) * 0.5
    dh_dy = (neighbor_field(0, 1) - neighbor_field(0, -1)) * 0.5

    strength = spec.normal_strength
    nx = -strength * dh_dx
    ny = -strength * dh_dy
    nz = np.ones_like(field)
    norm = np.sqrt(nx * nx + ny * ny + nz * nz)
    normal = np.stack([nx / norm, ny / norm, nz / norm], axis=-1)

    channels: dict[str, np.ndarray] = {
        "height": field,
        "roughness": roughness,
        "color": color,
        "normal": normal,
    }
    return {name: channels[name] for name in channels if name == "height" or name in spec.channels.names}
