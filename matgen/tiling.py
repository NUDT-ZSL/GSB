"""分块规划、接缝归一化权重与整幅拼接。

分块采用“核心区 + 重叠边距”的方式：
* 噪声与通道始终按全局坐标计算，分块只是在同一纯函数上取不同窗口；
* 重叠区域的权重在同一像素上对所有覆盖块归一化（和为 1），
  因此拼接在数学上等于整幅直接生成，浮点误差仅来自最终 dtype 转换。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .channels import build_channels
from .model import MaterialSpec


@dataclass(frozen=True)
class ChunkPlan:
    index: int
    row: int
    col: int
    core_x: int
    core_y: int
    core_w: int
    core_h: int
    x0: int
    y0: int
    tile_w: int
    tile_h: int


def plan_chunks(spec: MaterialSpec) -> list[ChunkPlan]:
    """按行列网格规划分块，生成含重叠边距的取数窗口。"""
    rows, cols = spec.grid_rows, spec.grid_cols
    core_w = spec.width // cols
    core_h = spec.height // rows
    overlap = spec.overlap

    plans: list[ChunkPlan] = []
    index = 0
    for row in range(rows):
        for col in range(cols):
            core_x = col * core_w
            core_y = row * core_h
            x0 = max(0, core_x - (overlap if col > 0 else 0))
            y0 = max(0, core_y - (overlap if row > 0 else 0))
            x1 = min(spec.width, core_x + core_w + (overlap if col < cols - 1 else 0))
            y1 = min(spec.height, core_y + core_h + (overlap if row < rows - 1 else 0))
            plans.append(
                ChunkPlan(
                    index=index,
                    row=row,
                    col=col,
                    core_x=core_x,
                    core_y=core_y,
                    core_w=core_w,
                    core_h=core_h,
                    x0=x0,
                    y0=y0,
                    tile_w=x1 - x0,
                    tile_h=y1 - y0,
                )
            )
            index += 1
    return plans


def axis_weights(start: int, length: int, core_start: int, core_end: int, limit: int, overlap: int) -> np.ndarray:
    """单轴线性斜坡权重。

    对长度为 ``n`` 的重叠区，左侧块右斜坡取 ``1-(k+1)/(n+1)``，
    右侧块左斜坡取 ``(k+1)/(n+1)``，同像素两者严格互补为 1。
    拼接时再按覆盖块权重和归一化，角点（两轴同时重叠）也保持单位分解。
    """
    weights = np.ones(length, dtype=np.float64)
    # 左重叠区：本块作为右侧块，权重从 1/(n+1) 升到 n/(n+1)。
    if start < core_start:
        left_len = core_start - start
        for k in range(left_len):
            weights[k] = (k + 1.0) / (left_len + 1.0)
    # 右重叠区：本块作为左侧块，权重从 n/(n+1) 降到 1/(n+1)。
    end = start + length
    if end > core_end:
        right_len = end - core_end
        for k in range(right_len):
            weights[length - 1 - k] = 1.0 - (k + 1.0) / (right_len + 1.0)
    return weights


def tile_weights(plan: ChunkPlan, spec: MaterialSpec) -> np.ndarray:
    wx = axis_weights(
        plan.x0,
        plan.tile_w,
        plan.core_x,
        plan.core_x + plan.core_w,
        spec.width,
        spec.overlap,
    )
    wy = axis_weights(
        plan.y0,
        plan.tile_h,
        plan.core_y,
        plan.core_y + plan.core_h,
        spec.height,
        spec.overlap,
    )
    return np.outer(wy, wx)


def render_tile(spec: MaterialSpec, plan: ChunkPlan) -> dict:
    """在全局坐标下渲染单个分块（含重叠边距）并附上归一化权重。"""
    channels = build_channels(spec, width=plan.tile_w, height=plan.tile_h, x0=plan.x0, y0=plan.y0)
    weights = tile_weights(plan, spec)
    return {
        "index": plan.index,
        "row": plan.row,
        "col": plan.col,
        "x0": plan.x0,
        "y0": plan.y0,
        "tile_w": plan.tile_w,
        "tile_h": plan.tile_h,
        "core_x": plan.core_x,
        "core_y": plan.core_y,
        "core_w": plan.core_w,
        "core_h": plan.core_h,
        "weights": weights,
        "channels": channels,
    }


def stitch(tiles: list[dict], spec: MaterialSpec, dtype=np.float64) -> dict[str, np.ndarray]:
    """按归一化权重把分块拼回整幅，输出各通道 (H,W,C) 或 (H,W) 数组。"""
    names = sorted({name for tile in tiles for name in tile["channels"].keys()})
    accum: dict[str, np.ndarray] = {}
    weight_sum = np.zeros((spec.height, spec.width), dtype=np.float64)

    for tile in sorted(tiles, key=lambda t: t["index"]):
        weights = tile["weights"].astype(np.float64)
        x0, y0 = tile["x0"], tile["y0"]
        th, tw = weights.shape
        weight_sum[y0 : y0 + th, x0 : x0 + tw] += weights
        for name in names:
            data = tile["channels"][name].astype(np.float64)
            if name not in accum:
                shape = (spec.height, spec.width) if data.ndim == 2 else (spec.height, spec.width, data.shape[2])
                accum[name] = np.zeros(shape, dtype=np.float64)
            broad = weights[..., None] if data.ndim == 3 else weights
            accum[name][y0 : y0 + th, x0 : x0 + tw] += broad * data

    denominator = weight_sum[..., None]
    result: dict[str, np.ndarray] = {}
    for name in names:
        data = accum[name]
        result[name] = (data / denominator if data.ndim == 3 else data / weight_sum).astype(dtype)
    return result


def compare_arrays(reference: np.ndarray, candidate: np.ndarray, tolerance: float):
    """逐像素裁决，返回 (是否一致, 首个不一致坐标, 最大误差)。"""
    diff = np.abs(reference.astype(np.float64) - candidate.astype(np.float64))
    pixel_error = diff.max(axis=tuple(range(2, diff.ndim))) if diff.ndim == 3 else diff
    max_error = float(pixel_error.max())
    if max_error <= tolerance:
        return True, None, max_error
    y, x = np.unravel_index(int(np.argmax(pixel_error)), pixel_error.shape)
    return False, (int(x), int(y)), max_error


def chunk_index_at(spec: MaterialSpec, x: int, y: int) -> int:
    """给定全局像素坐标，返回其核心区所属分块索引。"""
    core_w = spec.width // spec.grid_cols
    core_h = spec.height // spec.grid_rows
    col = min(x // core_w, spec.grid_cols - 1)
    row = min(y // core_h, spec.grid_rows - 1)
    return row * spec.grid_cols + col
