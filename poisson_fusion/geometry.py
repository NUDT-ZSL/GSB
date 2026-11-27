"""Mask validation, boundary extraction and band annotations."""

from __future__ import annotations

from collections import deque
from typing import Any

import numpy as np

from .errors import FusionError


def erode4(mask: np.ndarray) -> np.ndarray:
    padded = np.pad(mask.astype(bool), 1, mode="constant", constant_values=False)
    return (
        padded[1:-1, 1:-1]
        & padded[:-2, 1:-1]
        & padded[2:, 1:-1]
        & padded[1:-1, :-2]
        & padded[1:-1, 2:]
    )


def inner_boundary(mask: np.ndarray) -> np.ndarray:
    return mask.astype(bool) & ~erode4(mask)


def connected_components_4(mask: np.ndarray) -> list[np.ndarray]:
    mask = mask.astype(bool)
    visited = np.zeros(mask.shape, dtype=bool)
    components: list[np.ndarray] = []
    height, width = mask.shape
    for start_y, start_x in np.argwhere(mask):
        y, x = int(start_y), int(start_x)
        if visited[y, x]:
            continue
        component = np.zeros_like(mask, dtype=bool)
        queue: deque[tuple[int, int]] = deque([(y, x)])
        visited[y, x] = True
        while queue:
            cy, cx = queue.popleft()
            component[cy, cx] = True
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not visited[ny, nx]:
                    visited[ny, nx] = True
                    queue.append((ny, nx))
        components.append(component)
    return components


def validate_mask(mask: np.ndarray, image_shape: tuple[int, int] | None = None) -> dict[str, Any]:
    mask = np.asarray(mask)
    if mask.ndim != 2:
        raise FusionError(
            "E_MASK_SHAPE",
            "掩码必须为二维数组",
            {"mask_shape": list(mask.shape)},
        )
    if image_shape is not None and tuple(mask.shape) != tuple(image_shape):
        raise FusionError(
            "E_DIMENSIONS",
            "掩码与底板尺寸不一致",
            {"mask_shape": list(mask.shape), "image_shape": list(image_shape)},
        )
    boolean_mask = mask.astype(bool)
    height, width = boolean_mask.shape
    count = int(np.count_nonzero(boolean_mask))
    if count == 0:
        raise FusionError("E_EMPTY_MASK", "掩码为空，未指定融合区域", {"pixels": 0})
    if count == height * width:
        raise FusionError(
            "E_FULL_MASK",
            "掩码覆盖整幅图像，缺少底板边界条件",
            {"pixels": count, "shape": [height, width]},
        )
    if not erode4(boolean_mask).any():
        raise FusionError(
            "E_BROKEN_BOUNDARY",
            "掩码过窄，内部没有可求解像素",
            {"mask_pixels": count},
        )

    border_hits = int(
        np.count_nonzero(boolean_mask[0, :])
        + np.count_nonzero(boolean_mask[-1, :])
        + np.count_nonzero(boolean_mask[:, 0])
        + np.count_nonzero(boolean_mask[:, -1])
    )
    if border_hits:
        location = np.argwhere(
            boolean_mask
            & np.pad(np.ones((max(height - 2, 0), max(width - 2, 0)), dtype=bool), 1, constant_values=False)
        )
        first = [int(location[0, 0]), int(location[0, 1])] if location.size else None
        raise FusionError(
            "E_BROKEN_BOUNDARY",
            "掩码接触图像边缘，边界不闭合",
            {"border_pixels": border_hits, "first_border_pixel": first},
        )

    components = connected_components_4(boolean_mask)
    if len(components) != 1:
        sizes = sorted((int(np.count_nonzero(component)) for component in components), reverse=True)
        raise FusionError(
            "E_BROKEN_BOUNDARY",
            "掩码包含多个不连通融合区域，单零件掩码要求一个闭合区域",
            {"component_count": len(components), "component_sizes": sizes},
        )

    boundary = inner_boundary(boolean_mask)
    unknown = erode4(boolean_mask)
    return {
        "mask_pixels": count,
        "boundary_pixels": int(np.count_nonzero(boundary)),
        "unknown_pixels": int(np.count_nonzero(unknown)),
        "component_count": 1,
    }


def make_band(boundary: np.ndarray, radius: int) -> np.ndarray:
    if radius < 0:
        raise ValueError("band radius must be non-negative")
    boundary = np.asarray(boundary, dtype=bool)
    if not boundary.any() or radius == 0:
        return boundary.copy()
    height, width = boundary.shape
    band = np.zeros_like(boundary)
    distance = np.full(boundary.shape, -1, dtype=np.int16)
    queue: deque[tuple[int, int, int]] = deque()
    for y, x in np.argwhere(boundary):
        iy, ix = int(y), int(x)
        distance[iy, ix] = 0
        band[iy, ix] = True
        queue.append((iy, ix, 0))
    while queue:
        y, x, dist = queue.popleft()
        if dist >= radius:
            continue
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= ny < height and 0 <= nx < width and distance[ny, nx] < 0:
                distance[ny, nx] = dist + 1
                band[ny, nx] = True
                queue.append((ny, nx, dist + 1))
    return band


def bounding_box(mask: np.ndarray, margin: int = 0) -> dict[str, int] | None:
    mask = np.asarray(mask, dtype=bool)
    if not mask.any():
        return None
    ys, xs = np.nonzero(mask)
    height, width = mask.shape
    x0 = max(0, int(xs.min()) - margin)
    y0 = max(0, int(ys.min()) - margin)
    x1 = min(width, int(xs.max()) + 1 + margin)
    y1 = min(height, int(ys.max()) + 1 + margin)
    return {
        "x": x0,
        "y": y0,
        "width": x1 - x0,
        "height": y1 - y0,
        "x2": x1,
        "y2": y1,
        "pixels": int(np.count_nonzero(mask)),
    }


def region_statistics(mask: np.ndarray) -> dict[str, Any]:
    mask = np.asarray(mask, dtype=bool)
    return {"pixels": int(np.count_nonzero(mask)), "bbox": bounding_box(mask)}
