from __future__ import annotations

import math
from typing import Any

import numpy as np

from .contracts import output_spatial_shape


def _reference_index(index: int, size: int, boundary: str) -> tuple[int, bool]:
    if 0 <= index < size:
        return index, True
    if boundary == "zero":
        return 0, False
    if boundary == "replicate":
        return min(size - 1, max(0, index)), True
    if boundary == "reflect":
        if size == 1:
            return 0, True
        period = 2 * (size - 1)
        wrapped = index % period
        if wrapped >= size:
            wrapped = period - wrapped
        return int(wrapped), True
    raise ValueError(f"unsupported boundary mode: {boundary}")


def _cubic_weight(distance: float, a: float = -0.5) -> float:
    x = abs(float(distance))
    if x <= 1.0:
        return (a + 2.0) * x**3 - (a + 3.0) * x**2 + 1.0
    if x < 2.0:
        return a * x**3 - 5.0 * a * x**2 + 8.0 * a * x - 4.0 * a
    return 0.0


def reference_conv2d(task: dict[str, Any], arrays: dict[str, np.ndarray]) -> np.ndarray:
    x = arrays["input"].astype(np.float64, copy=False)
    w = arrays["weight"].astype(np.float64, copy=False)
    n_batch, channels, height, width = x.shape
    kernel_h, kernel_w = (int(v) for v in task["kernel_shape"])
    stride_h, stride_w = (int(v) for v in task["stride"])
    pad_h, pad_w = (int(v) for v in task["padding"])
    out_h, out_w = output_spatial_shape(task)
    boundary = task["block_policy"]["boundary"]
    output = np.zeros((n_batch, channels, out_h, out_w), dtype=np.float64)

    for n in range(n_batch):
        for c in range(channels):
            for oh in range(out_h):
                for ow in range(out_w):
                    total = 0.0
                    for kh in range(kernel_h):
                        ih = oh * stride_h - pad_h + kh
                        ref_ih, ok_h = _reference_index(ih, height, boundary)
                        for kw in range(kernel_w):
                            iw = ow * stride_w - pad_w + kw
                            ref_iw, ok_w = _reference_index(iw, width, boundary)
                            if ok_h and ok_w:
                                total += float(x[n, c, ref_ih, ref_iw]) * float(w[kh, kw])
                    output[n, c, oh, ow] = total
    return output


def _coordinate(output_index: int, scale: float) -> float:
    return output_index / scale


def reference_interpolate(task: dict[str, Any], arrays: dict[str, np.ndarray]) -> np.ndarray:
    x = arrays["input"].astype(np.float64, copy=False)
    mode = task["interp_mode"]
    scale_h, scale_w = (float(v) for v in task["scale"])
    boundary = task["block_policy"]["boundary"]
    n_batch, channels, height, width = x.shape
    out_h, out_w = output_spatial_shape(task)
    output = np.zeros((n_batch, channels, out_h, out_w), dtype=np.float64)

    for n in range(n_batch):
        for c in range(channels):
            for oy in range(out_h):
                sy = _coordinate(oy, scale_h)
                for ox in range(out_w):
                    sx = _coordinate(ox, scale_w)
                    if mode == "nearest":
                        iy = int(math.floor(sy + 0.5))
                        ix = int(math.floor(sx + 0.5))
                        ry, ok_y = _reference_index(iy, height, boundary)
                        rx, ok_x = _reference_index(ix, width, boundary)
                        if ok_y and ok_x:
                            output[n, c, oy, ox] = float(x[n, c, ry, rx])

                    elif mode == "bilinear":
                        y0 = int(math.floor(sy))
                        x0 = int(math.floor(sx))
                        fy = sy - y0
                        fx = sx - x0
                        value = 0.0
                        for dy, wy in ((0, 1.0 - fy), (1, fy)):
                            ry, ok_y = _reference_index(y0 + dy, height, boundary)
                            for dx, wx in ((0, 1.0 - fx), (1, fx)):
                                rx, ok_x = _reference_index(x0 + dx, width, boundary)
                                if ok_y and ok_x:
                                    value += wy * wx * float(x[n, c, ry, rx])
                        output[n, c, oy, ox] = value

                    elif mode == "bicubic":
                        y_base = int(math.floor(sy)) - 1
                        x_base = int(math.floor(sx)) - 1
                        ys = [_reference_index(y_base + j, height, boundary) for j in range(4)]
                        xs = [_reference_index(x_base + i, width, boundary) for i in range(4)]
                        wy = [_cubic_weight(sy - (y_base + j)) for j in range(4)]
                        wx = [_cubic_weight(sx - (x_base + i)) for i in range(4)]
                        value = 0.0
                        for j in range(4):
                            ry, ok_y = ys[j]
                            if not ok_y:
                                continue
                            for i in range(4):
                                rx, ok_x = xs[i]
                                if ok_x:
                                    value += wy[j] * wx[i] * float(x[n, c, ry, rx])
                        output[n, c, oy, ox] = value
                    else:  # pragma: no cover - normalized by contracts
                        raise ValueError(f"unsupported interpolation mode: {mode}")
    return output


def reference_task(task: dict[str, Any], arrays: dict[str, np.ndarray]) -> np.ndarray:
    if task["op_kind"] == "conv2d":
        return reference_conv2d(task, arrays)
    return reference_interpolate(task, arrays)
