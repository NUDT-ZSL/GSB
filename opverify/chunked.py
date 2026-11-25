from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .planner import build_plan, resolve_indices


def _cubic_weights(distance: np.ndarray, a: float = -0.5) -> np.ndarray:
    abs_d = np.abs(distance)
    near = (a + 2.0) * abs_d**3 - (a + 3.0) * abs_d**2 + 1.0
    far = a * abs_d**3 - 5.0 * a * abs_d**2 + 8.0 * a * abs_d - 4.0 * a
    return np.where(abs_d <= 1.0, near, np.where(abs_d < 2.0, far, 0.0))


def _gather(
    image: np.ndarray,
    y_indices: np.ndarray,
    x_indices: np.ndarray,
    height: int,
    width: int,
    boundary: str,
) -> np.ndarray:
    y_map, y_valid = resolve_indices(y_indices, height, boundary)
    x_map, x_valid = resolve_indices(x_indices, width, boundary)
    values = image[y_map, x_map].astype(np.float64, copy=False)
    return values * y_valid.astype(np.float64) * x_valid.astype(np.float64)


def _conv_tile(
    image: np.ndarray,
    weight: np.ndarray,
    task: dict[str, Any],
    y0: int,
    y1: int,
    x0: int,
    x1: int,
) -> np.ndarray:
    height, width = image.shape
    kernel_h, kernel_w = task["kernel_shape"]
    stride_h, stride_w = task["stride"]
    pad_h, pad_w = task["padding"]
    boundary = task["block_policy"]["boundary"]
    oy, ox = np.mgrid[y0:y1, x0:x1]
    accumulator = np.zeros((y1 - y0, x1 - x0), dtype=np.float64)

    for kh in range(kernel_h):
        ih = oy * stride_h - pad_h + kh
        y_map, y_valid = resolve_indices(ih, height, boundary)
        for kw in range(kernel_w):
            iw = ox * stride_w - pad_w + kw
            x_map, x_valid = resolve_indices(iw, width, boundary)
            sampled = image[y_map, x_map].astype(np.float64, copy=False)
            mask = y_valid.astype(np.float64) * x_valid.astype(np.float64)
            accumulator += sampled * mask * float(weight[kh, kw])
    return accumulator


def _interpolate_tile(
    image: np.ndarray,
    task: dict[str, Any],
    y0: int,
    y1: int,
    x0: int,
    x1: int,
) -> np.ndarray:
    height, width = image.shape
    scale_h, scale_w = task["scale"]
    mode = task["interp_mode"]
    boundary = task["block_policy"]["boundary"]
    oy, ox = np.mgrid[y0:y1, x0:x1]
    sy = oy.astype(np.float64) / scale_h
    sx = ox.astype(np.float64) / scale_w

    if mode == "nearest":
        iy = np.floor(sy + 0.5).astype(np.int64)
        ix = np.floor(sx + 0.5).astype(np.int64)
        return _gather(image, iy, ix, height, width, boundary)

    if mode == "bilinear":
        y_base = np.floor(sy).astype(np.int64)
        x_base = np.floor(sx).astype(np.int64)
        fy = sy - y_base
        fx = sx - x_base
        v00 = _gather(image, y_base, x_base, height, width, boundary)
        v01 = _gather(image, y_base, x_base + 1, height, width, boundary)
        v10 = _gather(image, y_base + 1, x_base, height, width, boundary)
        v11 = _gather(image, y_base + 1, x_base + 1, height, width, boundary)
        return (
            (1.0 - fy) * (1.0 - fx) * v00
            + (1.0 - fy) * fx * v01
            + fy * (1.0 - fx) * v10
            + fy * fx * v11
        )

    y_floor = np.floor(sy).astype(np.int64)
    x_floor = np.floor(sx).astype(np.int64)
    y_base = y_floor - 1
    x_base = x_floor - 1
    result = np.zeros_like(sy, dtype=np.float64)
    for j in range(4):
        y_index = y_base + j
        wy = _cubic_weights(sy - y_index)
        for i in range(4):
            x_index = x_base + i
            wx = _cubic_weights(sx - x_index)
            values = _gather(image, y_index, x_index, height, width, boundary)
            result += wy * wx * values
    return result


def run_chunked(
    task: dict[str, Any],
    arrays: dict[str, np.ndarray],
    plan: dict[str, Any] | None = None,
    temp_dir: str | Path | None = None,
) -> np.ndarray:
    tile_plan = plan or build_plan(task)
    x = arrays["input"].astype(np.float64, copy=False)
    weight = arrays.get("weight")
    if weight is not None:
        weight = weight.astype(np.float64, copy=False)

    output = np.zeros(tuple(task["output_shape"]), dtype=np.float64)
    temporary = Path(temp_dir) if temp_dir is not None else None
    if temporary is not None:
        temporary.mkdir(parents=True, exist_ok=True)

    for tile in tile_plan["tiles"]:
        y0, y1 = int(tile["y0"]), int(tile["y1"])
        x0, x1 = int(tile["x0"]), int(tile["x1"])
        for n in range(x.shape[0]):
            for c in range(x.shape[1]):
                if task["op_kind"] == "conv2d":
                    assert weight is not None
                    values = _conv_tile(
                        x[n, c], weight, task, y0, y1, x0, x1
                    )
                else:
                    values = _interpolate_tile(x[n, c], task, y0, y1, x0, x1)
                output[n, c, y0:y1, x0:x1] = values
        if temporary is not None:
            tile_bytes = np.ascontiguousarray(
                output[:, :, y0:y1, x0:x1]
            ).astype("<f8", copy=False).tobytes(order="C")
            (temporary / f"{tile['tile_id']}.bin").write_bytes(tile_bytes)
    return output
