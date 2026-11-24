"""确定性多倍频梯度噪声。

设计要点：
* 任意格点的取值只由材质种子、频率、振幅、倍频数和全局坐标决定，
  与调用顺序、线程、分块划分无关，因此整幅与分块逐格相同。
* 全部使用固定位宽整数哈希与 numpy 向量化，不依赖全局随机状态。
"""

from __future__ import annotations

import numpy as np

# 8 个三维梯度，z 维恒取 0，等效 2D 梯度表。
_GRADIENTS = (
    (1, 1, 0),
    (-1, 1, 0),
    (1, -1, 0),
    (-1, -1, 0),
    (1, 0, 1),
    (-1, 0, 1),
    (0, 1, -1),
    (0, -1, -1),
)
_MASK64 = np.uint64(0xFFFFFFFFFFFFFFFF)


def _hash_index(seed: int, ix: np.ndarray, iy: np.ndarray) -> np.ndarray:
    """种子 + 晶格坐标 -> [0,8) 稳定索引，位宽固定以保证跨平台一致。"""
    h = np.uint64(seed) + (ix.astype(np.uint64) * np.uint64(0x9E3779B97F4A7C15))
    h = (h ^ (iy.astype(np.uint64) + np.uint64(0xC2B2AE3D27D4EB4F))) & _MASK64
    h = (h * np.uint64(0xBF58476D1CE4E5B9) + np.uint64(0x94D049BB133111EB)) & _MASK64
    h ^= h >> np.uint64(31)
    h = (h * np.uint64(0x9E3779B185EBCA87)) & _MASK64
    h ^= h >> np.uint64(33)
    return (h & np.uint64(7)).astype(np.intp)


def _gradient_dot(ix, iy, fx, fy) -> np.ndarray:
    idx = _hash_index(0, ix, iy)  # 种子在 _perlin 层统一注入到坐标偏移中
    gx = np.choose(idx, [g[0] for g in _GRADIENTS])
    gy = np.choose(idx, [g[1] for g in _GRADIENTS])
    return gx * fx + gy * fy


def _fade(t: np.ndarray) -> np.ndarray:
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


def _perlin(coords_x: np.ndarray, coords_y: np.ndarray, seed: int) -> np.ndarray:
    """对任意形状坐标网格计算 Perlin 噪声（约在 [-1,1]）。"""
    # 种子通过整体晶格平移注入，保持纯函数特性且避免与原点退化对齐。
    shift = (int(seed) * 374761393) & 0x7FFFFFFF
    x = coords_x + shift
    y = coords_y + ((int(seed) * 668265263) & 0x7FFFFFFF)

    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    x1 = x0 + 1
    y1 = y0 + 1
    sx = _fade(x - x0)
    sy = _fade(y - y0)

    n00 = _gradient_dot(x0, y0, x - x0, y - y0)
    n10 = _gradient_dot(x1, y0, x - x1, y - y0)
    n01 = _gradient_dot(x0, y1, x - x0, y - y1)
    n11 = _gradient_dot(x1, y1, x - x1, y - y1)

    nx0 = n00 + sx * (n10 - n00)
    nx1 = n01 + sx * (n11 - n01)
    return nx0 + sy * (nx1 - nx0)


def fbm_field(
    width: int,
    height: int,
    seed: int,
    frequencies,
    amplitudes,
    x0: int = 0,
    y0: int = 0,
) -> np.ndarray:
    """生成 (height, width) 的多倍频叠加场，可按全局偏移 (x0,y0) 取任意分块。"""
    xs = np.arange(x0, x0 + width, dtype=np.float64)
    ys = np.arange(y0, y0 + height, dtype=np.float64)
    grid_x, grid_y = np.meshgrid(xs, ys)

    total = np.zeros((height, width), dtype=np.float64)
    weight_sum = 0.0
    for frequency, amplitude in zip(frequencies, amplitudes):
        # 频率以“每像素周期数”表达，乘以固定 2pi 转换为连续坐标。
        total += float(amplitude) * _perlin(grid_x * float(frequency), grid_y * float(frequency), seed)
        weight_sum += float(amplitude)
    return total / weight_sum


def fbm_point(x: int, y: int, seed: int, frequencies, amplitudes) -> float:
    """单格点取值；与 fbm_field 同坐标时严格一致，便于做不变量测试。"""
    gx = np.array([[float(x)]], dtype=np.float64)
    gy = np.array([[float(y)]], dtype=np.float64)
    total = 0.0
    weight_sum = 0.0
    for frequency, amplitude in zip(frequencies, amplitudes):
        total += float(amplitude) * float(_perlin(gx * float(frequency), gy * float(frequency), seed)[0, 0])
        weight_sum += float(amplitude)
    return total / weight_sum
