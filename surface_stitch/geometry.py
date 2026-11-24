"""Small deterministic geometry and bilinear resampling helpers."""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import numpy as np


Rect = Dict[str, int]


def intersection(a: Rect, b: Rect) -> Optional[Tuple[int, int, int, int]]:
    """Return ``(x0, y0, x1, y1)`` intersection in canvas coordinates."""

    x0 = max(int(a["x"]), int(b["x"]))
    y0 = max(int(a["y"]), int(b["y"]))
    x1 = min(int(a["x"]) + int(a["width"]), int(b["x"]) + int(b["width"]))
    y1 = min(int(a["y"]) + int(a["height"]), int(b["y"]) + int(b["height"]))
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def rect_tuple(rect: Rect) -> Tuple[int, int, int, int]:
    return (
        int(rect["x"]),
        int(rect["y"]),
        int(rect["x"]) + int(rect["width"]),
        int(rect["y"]) + int(rect["height"]),
    )


def bilinear_shift_image(
    source: np.ndarray,
    dx: float,
    dy: float,
    out_shape: Optional[Tuple[int, int]] = None,
) -> np.ndarray:
    """Resample ``destination[y, x] = source[y + dy?]``.

    The pipeline convention is ``source_coordinate = destination - offset``.
    Thus an offset ``dx=2`` means destination column 0 asks source column
    -2 and is zero-filled.  Bilinear taps outside the source are treated as
    zero, which is the single resampling convention used by every block.
    """

    src = np.asarray(source, dtype=np.float64)
    out_h, out_w = out_shape or src.shape
    dst_y, dst_x = np.mgrid[0:out_h, 0:out_w].astype(np.float64)
    source_x = dst_x - float(dx)
    source_y = dst_y - float(dy)

    x0 = np.floor(source_x).astype(np.int64)
    y0 = np.floor(source_y).astype(np.int64)
    fx = source_x - x0
    fy = source_y - y0

    def tap(ix: np.ndarray, iy: np.ndarray) -> np.ndarray:
        valid = (ix >= 0) & (ix < src.shape[1]) & (iy >= 0) & (iy < src.shape[0])
        values = np.zeros(ix.shape, dtype=np.float64)
        if np.any(valid):
            values[valid] = src[iy[valid], ix[valid]]
        return values

    v00 = tap(x0, y0)
    v10 = tap(x0 + 1, y0)
    v01 = tap(x0, y0 + 1)
    v11 = tap(x0 + 1, y0 + 1)

    top = v00 * (1.0 - fx) + v10 * fx
    bottom = v01 * (1.0 - fx) + v11 * fx
    return top * (1.0 - fy) + bottom * fy


def slice_for_shift(
    height: int, width: int, dy: int, dx: int
) -> Tuple[slice, slice, slice, slice]:
    """Return ref/moving slices corresponding to relation B[k]=A[k+(dy,dx)]."""

    if dy >= 0:
        ref_y = slice(dy, height)
        mov_y = slice(0, height - dy)
    else:
        ref_y = slice(0, height + dy)
        mov_y = slice(-dy, height)

    if dx >= 0:
        ref_x = slice(dx, width)
        mov_x = slice(0, width - dx)
    else:
        ref_x = slice(0, width + dx)
        mov_x = slice(-dx, width)

    return ref_y, ref_x, mov_y, mov_x
