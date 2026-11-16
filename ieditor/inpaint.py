"""Deterministic layered inpainting using fixed-point inverse-square weights.

The damaged region is filled from the outside inward. Each layer is the set of
damaged pixels adjacent (4-connectivity) to already-known pixels. All pixels of
one layer are computed from the known pixels of previous layers only, which
guarantees order-independent, reproducible results. Arithmetic is strictly
integer fixed-point (scale 2**16); no float is used for pixel values.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List

import numpy as np

from .config import RenderConfig
from .errors import FormatInvalid
from .models import MASK_DAMAGED, MASK_INTACT, UnifiedProject


@dataclass
class LayerStat:
    layer_index: int
    pixel_count: int
    source_pixel_count: int
    weight_sum: int
    coordinates: List[List[int]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layer_index": self.layer_index,
            "pixel_count": self.pixel_count,
            "source_pixel_count": self.source_pixel_count,
            "weight_sum": self.weight_sum,
            "coordinates": self.coordinates,
        }


@dataclass
class InpaintResult:
    image: np.ndarray
    layers: List[LayerStat]
    filled_pixel_count: int
    fixed_point_scale: int

    def stats(self) -> Dict[str, Any]:
        return {
            "filled_pixel_count": self.filled_pixel_count,
            "layer_count": len(self.layers),
            "fixed_point_scale": self.fixed_point_scale,
            "layers": [layer.to_dict() for layer in self.layers],
        }


def _layer_pixels(damaged: np.ndarray, known: np.ndarray,
                  height: int, width: int) -> List[tuple]:
    """Damaged pixels with at least one 4-connected known neighbor."""
    layer: List[tuple] = []
    damaged_rows, damaged_cols = np.nonzero(damaged)
    for row, col in zip(damaged_rows.tolist(), damaged_cols.tolist()):
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ny, nx = row + dy, col + dx
            if 0 <= ny < height and 0 <= nx < width and known[ny, nx]:
                layer.append((row, col))
                break
    layer.sort()  # fixed row-major tie rule
    return layer


def inpaint(project: UnifiedProject, image: np.ndarray,
            config: RenderConfig) -> InpaintResult:
    if image.ndim != 3 or image.shape[2] != 3:
        raise FormatInvalid("修复输入必须为 RGB 三通道图像",
                            context={"shape": list(image.shape)})
    height, width = project.mask.shape
    if image.shape[0] != height or image.shape[1] != width:
        raise FormatInvalid(
            "图像尺寸与掩码尺寸不一致",
            context={"image_shape": list(image.shape),
                     "mask_shape": [height, width]})

    radius = int(config.neighborhood_radius)
    scale = int(config.fixed_point_scale)
    work = image.astype(np.int64, copy=True)
    damaged = project.mask == MASK_DAMAGED
    known = ~damaged
    remaining = damaged.copy()

    # Fixed deterministic neighbor offsets: row-major within the square window.
    offsets = [(dy, dx) for dy in range(-radius, radius + 1)
               for dx in range(-radius, radius + 1)
               if not (dy == 0 and dx == 0)]

    layers: List[LayerStat] = []
    layer_index = 0
    total_filled = 0

    while True:
        layer = _layer_pixels(remaining, known, height, width)
        if not layer:
            break
        updates: List[tuple] = []
        for row, col in layer:
            accum = [0, 0, 0]
            weight_sum = 0
            source_pixels: List[tuple] = []
            for dy, dx in offsets:  # fixed row-major neighbor order
                ny, nx = row + dy, col + dx
                # Out-of-bounds pixels: skip deterministically.
                if not (0 <= ny < height and 0 <= nx < width):
                    continue
                if not known[ny, nx]:
                    continue
                weight = scale // (dy * dy + dx * dx)
                for channel in range(3):
                    accum[channel] += int(work[ny, nx, channel]) * weight
                weight_sum += weight
                source_pixels.append((ny, nx))
            if weight_sum == 0:
                # Fixed fallback rule: nearest known pixel in row-major order,
                # then the global top-left known pixel.
                fallback = _fallback_pixel(known, height, width, row, col)
                if fallback is None:
                    raise FormatInvalid(
                        "缺损区域无任何可用来源像素，无法修复",
                        context={"coord": [row, col]})
                fy, fx = fallback
                updates.append((row, col,
                                [int(work[fy, fx, c]) for c in range(3)],
                                0, 1, [[fy, fx]]))
                continue
            filled = [_divide_nearest(accum[c], weight_sum) for c in range(3)]
            updates.append((row, col, filled, weight_sum,
                            len(source_pixels),
                            [list(p) for p in source_pixels]))

        for row, col, filled, weight_sum, source_count, _src in updates:
            for channel in range(3):
                work[row, col, channel] = filled[channel]
            remaining[row, col] = False
            known[row, col] = True
        stat = LayerStat(
            layer_index=layer_index,
            pixel_count=len(updates),
            source_pixel_count=sum(u[4] for u in updates),
            weight_sum=sum(u[3] for u in updates),
            coordinates=[[u[0], u[1]] for u in updates],
        )
        layers.append(stat)
        total_filled += len(updates)
        layer_index += 1

    if remaining.any():
        leftover = np.argwhere(remaining)
        raise FormatInvalid(
            "存在无法分层到达的缺损像素",
            context={"coord": leftover[0].tolist()})

    out = np.clip(work, 0, 255).astype(np.uint8)
    return InpaintResult(image=out, layers=layers,
                         filled_pixel_count=total_filled,
                         fixed_point_scale=scale)


def _divide_nearest(numerator: int, denominator: int) -> int:
    """Integer division with deterministic half-up rounding."""
    if numerator >= 0:
        return (numerator + denominator // 2) // denominator
    return -((-numerator + denominator // 2) // denominator)


def _fallback_pixel(known: np.ndarray, height: int, width: int,
                    row: int, col: int):
    best = None
    best_dist = None
    rows, cols = np.nonzero(known)
    for ny, nx in zip(rows.tolist(), cols.tolist()):
        dist = (ny - row) ** 2 + (nx - col) ** 2
        if best_dist is None or dist < best_dist or (
                dist == best_dist and (ny, nx) < best):
            best = (ny, nx)
            best_dist = dist
    return best
