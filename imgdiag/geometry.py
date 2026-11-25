from __future__ import annotations

import math
from typing import Any

import numpy as np

from .errors import WorkbenchError
from .hashing import canonical_hash


Point = tuple[float, float]


def _point(value: Any) -> Point:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise WorkbenchError("E_SCHEMA_INVALID", "mask points must be [x, y]")
    try:
        x = float(value[0])
        y = float(value[1])
    except (TypeError, ValueError) as exc:
        raise WorkbenchError("E_SCHEMA_INVALID", "mask coordinates must be numbers") from exc
    if not (math.isfinite(x) and math.isfinite(y)):
        raise WorkbenchError("E_SCHEMA_INVALID", "mask coordinates must be finite")
    if abs(x - round(x)) < 1e-9:
        x = float(round(x))
    if abs(y - round(y)) < 1e-9:
        y = float(round(y))
    return x, y


def extract_polygons(mask: Any) -> list[list[Point]]:
    if isinstance(mask, dict) and "polygons" in mask:
        raw_polygons = mask["polygons"]
    elif isinstance(mask, dict) and "polygon" in mask:
        raw_polygons = [mask["polygon"]]
    elif isinstance(mask, list):
        if mask and isinstance(mask[0], (list, tuple)) and mask[0] and isinstance(mask[0][0], (list, tuple)):
            raw_polygons = mask
        else:
            raw_polygons = [mask]
    else:
        raise WorkbenchError("E_SCHEMA_INVALID", "mask must be a polygon or {'polygons': [...]}")

    polygons: list[list[Point]] = []
    for raw in raw_polygons:
        polygon = [_point(point) for point in raw]
        # GeoJSON-style explicit closure is accepted once.
        if len(polygon) >= 2 and polygon[0] == polygon[-1]:
            polygon = polygon[:-1]
        polygons.append(polygon)
    return polygons


def _edge_key(a: Point, b: Point) -> tuple[Point, Point]:
    return (a, b) if a <= b else (b, a)


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: Point, b: Point, c: Point, eps: float = 1e-9) -> bool:
    return (
        abs(_orient(a, b, c)) <= eps
        and min(a[0], b[0]) - eps <= c[0] <= max(a[0], b[0]) + eps
        and min(a[1], b[1]) - eps <= c[1] <= max(a[1], b[1]) + eps
    )


def _segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    o1 = _orient(a, b, c)
    o2 = _orient(a, b, d)
    o3 = _orient(c, d, a)
    o4 = _orient(c, d, b)
    eps = 1e-9
    if abs(o1) <= eps and _on_segment(a, b, c):
        return True
    if abs(o2) <= eps and _on_segment(a, b, d):
        return True
    if abs(o3) <= eps and _on_segment(c, d, a):
        return True
    if abs(o4) <= eps and _on_segment(c, d, b):
        return True
    return ((o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps)) and (
        (o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps)
    )


def validate_polygon(polygon: list[Point], width: int, height: int) -> None:
    if len(polygon) < 3:
        raise WorkbenchError("E_MASK_DEGENERATE", "polygon needs at least three vertices")

    for x, y in polygon:
        if x < 0.0 or y < 0.0 or x > float(width) or y > float(height):
            raise WorkbenchError(
                "E_MASK_OUT_OF_BOUNDS",
                "mask vertex lies outside [0, width] x [0, height]",
                coordinate=[x, y],
                canvas=[height, width],
            )

    seen: set[Point] = set()
    edge_keys: set[tuple[Point, Point]] = set()
    count = len(polygon)
    for index, point in enumerate(polygon):
        if point in seen:
            raise WorkbenchError(
                "E_MASK_DEGENERATE", "repeated polygon vertex", coordinate=list(point)
            )
        seen.add(point)
        nxt = polygon[(index + 1) % count]
        if point == nxt:
            raise WorkbenchError(
                "E_MASK_DEGENERATE", "zero-length/repeated mask edge", coordinate=list(point)
            )
        edge = _edge_key(point, nxt)
        if edge in edge_keys:
            raise WorkbenchError(
                "E_MASK_DEGENERATE", "duplicate mask edge", edge=[list(edge[0]), list(edge[1])]
            )
        edge_keys.add(edge)

    for i in range(count):
        a = polygon[i]
        b = polygon[(i + 1) % count]
        for j in range(i + 1, count):
            c = polygon[j]
            d = polygon[(j + 1) % count]
            # Adjacent edge pairs share one endpoint and are valid.
            if j == i + 1 or (i == 0 and j == count - 1):
                continue
            if _segments_intersect(a, b, c, d):
                raise WorkbenchError(
                    "E_MASK_INTERSECT",
                    "non-adjacent polygon edges intersect",
                    edges=[[list(a), list(b)], [list(c), list(d)]],
                )


def point_in_polygon(x: float, y: float, polygon: list[Point]) -> bool:
    inside = False
    count = len(polygon)
    for i in range(count):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % count]
        if _on_segment((x1, y1), (x2, y2), (x, y)):
            return True
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x_cross > x:
                inside = not inside
    return inside


def rasterize_polygon(polygon: list[Point], height: int, width: int) -> np.ndarray:
    mask = np.zeros((height, width), dtype=bool)
    if polygon:
        min_x = max(0, int(math.floor(min(x for x, _ in polygon))))
        max_x = min(width - 1, int(math.ceil(max(x for x, _ in polygon))))
        min_y = max(0, int(math.floor(min(y for _, y in polygon))))
        max_y = min(height - 1, int(math.ceil(max(y for _, y in polygon))))
        for y in range(min_y, max_y + 1):
            for x in range(min_x, max_x + 1):
                if point_in_polygon(float(x), float(y), polygon):
                    mask[y, x] = True
    return mask


def _neighbor_offsets() -> list[tuple[int, int]]:
    return [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy != 0 or dx != 0]


def build_mask_model(
    mask: Any,
    image_shape: tuple[int, int, int] | list[int],
    image: np.ndarray | None = None,
) -> dict[str, Any]:
    height, width = int(image_shape[0]), int(image_shape[1])
    polygons = extract_polygons(mask)
    for polygon in polygons:
        validate_polygon(polygon, width, height)

    offsets = _neighbor_offsets()
    regions: list[dict[str, Any]] = []
    union = np.zeros((height, width), dtype=bool)

    for region_index, polygon in enumerate(polygons):
        raster = rasterize_polygon(polygon, height, width)
        if not raster.any():
            raise WorkbenchError("E_MASK_DEGENERATE", "mask polygon rasterizes to no pixels")
        union |= raster
        coords = np.argwhere(raster)  # rows are [y, x], sorted row-major.
        coord_set = {tuple(map(int, coord)) for coord in coords}
        boundary_set: set[tuple[int, int]] = set()
        context_set: set[tuple[int, int]] = set()
        for y, x in coords:
            is_boundary = False
            for dy4, dx4 in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                ny, nx = int(y) + dy4, int(x) + dx4
                if not (0 <= ny < height and 0 <= nx < width) or (ny, nx) not in coord_set:
                    is_boundary = True
                    if 0 <= ny < height and 0 <= nx < width and (ny, nx) not in coord_set:
                        context_set.add((ny, nx))
            if is_boundary:
                boundary_set.add((int(y), int(x)))
            for dy, dx in offsets:
                ny, nx = int(y) + dy, int(x) + dx
                if 0 <= ny < height and 0 <= nx < width and (ny, nx) not in coord_set:
                    context_set.add((ny, nx))

        boundary = np.array(sorted(boundary_set), dtype=np.int64).reshape(-1, 2)
        context = np.array(sorted(context_set), dtype=np.int64).reshape(-1, 2)
        context_fallback = False
        if context.size == 0:
            # Defensive fallback for a mask covering the complete canvas.  It
            # keeps the operator total; ordinary samples always have a ring.
            context = np.array(
                [(0, x) for x in range(width)] + [(height - 1, x) for x in range(width)]
                + [(y, 0) for y in range(1, height - 1)]
                + [(y, width - 1) for y in range(1, height - 1)],
                dtype=np.int64,
            )
            context = np.unique(context, axis=0)
            context_fallback = True

        context_rgb = None
        if image is not None:
            context_rgb = image[context[:, 0], context[:, 1]].astype(np.float64)

        regions.append({
            "region_id": f"R{region_index}",
            "polygon": [list(point) for point in polygon],
            "pixel_coords": coords,
            "boundary_coords": boundary,
            "context_coords": context,
            "context_rgb": context_rgb,
            "context_fallback": context_fallback,
            "pixel_count": int(coords.shape[0]),
            "boundary_count": int(boundary.shape[0]),
            "context_count": int(context.shape[0]),
        })

    polygon_digest = canonical_hash(polygons)
    raster_digest = __import__("hashlib").sha256(np.ascontiguousarray(union).tobytes()).hexdigest()
    return {
        "height": height,
        "width": width,
        "regions": regions,
        "union_mask": union,
        "polygon_digest": polygon_digest,
        "mask_digest": canonical_hash({"polygons": polygons, "raster": raster_digest}),
        "region_count": len(regions),
    }
