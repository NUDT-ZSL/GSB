"""Analytic 2D geometry primitives used for sub-pixel coverage estimation.

All quantities in this module live in screen pixel space.  The goal is to
measure the exact area a thick segment (a rounded "capsule") covers inside a
unit pixel square, without stochastic sampling, so that features thinner than
a pixel remain stable between renders.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence, Tuple

import numpy as np


@dataclass(frozen=True)
class Segment:
    """An oriented straight segment with a radius (half width)."""

    x0: float
    y0: float
    x1: float
    y1: float
    radius: float

    @property
    def length(self) -> float:
        return math.hypot(self.x1 - self.x0, self.y1 - self.y0)


@dataclass(frozen=True)
class GridCell:
    """A pixel cell paired with the centre-line length crossing it."""

    ix: int
    iy: int
    t_enter: float
    t_exit: float

    @property
    def span(self) -> float:
        return self.t_exit - self.t_enter


def segment_grid_cells(x0: float, y0: float, x1: float, y1: float) -> List[GridCell]:
    """Enumerate every unit pixel cell crossed by a segment.

    Implements the Amanatides-Woo grid traversal; the returned cells carry the
    parametric span of the segment lying inside each cell.
    """
    dx = x1 - x0
    dy = y1 - y0
    length = math.hypot(dx, dy)
    if length == 0.0:
        ix, iy = int(math.floor(x0)), int(math.floor(y0))
        return [GridCell(ix, iy, 0.0, 1.0)]

    step_x = 1 if dx > 0 else (-1 if dx < 0 else 0)
    step_y = 1 if dy > 0 else (-1 if dy < 0 else 0)

    ix, iy = int(math.floor(x0)), int(math.floor(y0))
    end_ix, end_iy = int(math.floor(x1)), int(math.floor(y1))

    t_delta_x = abs(1.0 / dx) if dx != 0.0 else math.inf
    t_delta_y = abs(1.0 / dy) if dy != 0.0 else math.inf

    if dx != 0.0:
        boundary = ix + (1.0 if step_x > 0 else 0.0)
        t_max_x = (boundary - x0) / dx
    else:
        t_max_x = math.inf
    if dy != 0.0:
        boundary = iy + (1.0 if step_y > 0 else 0.0)
        t_max_y = (boundary - y0) / dy
    else:
        t_max_y = math.inf

    t_max_x = max(t_max_x, 0.0)
    t_max_y = max(t_max_y, 0.0)

    cells: List[GridCell] = []
    t_enter = 0.0
    guard = 0
    max_steps = abs(end_ix - ix) + abs(end_iy - iy) + 4
    while guard <= max_steps:
        guard += 1
        if step_x != 0 and (step_y == 0 or t_max_x < t_max_y):
            t_exit = min(t_max_x, 1.0)
            cells.append(GridCell(ix, iy, t_enter, t_exit))
            t_enter = t_exit
            ix += step_x
            t_max_x += t_delta_x
        elif step_y != 0:
            t_exit = min(t_max_y, 1.0)
            cells.append(GridCell(ix, iy, t_enter, t_exit))
            t_enter = t_exit
            iy += step_y
            t_max_y += t_delta_y
        else:
            cells.append(GridCell(ix, iy, t_enter, 1.0))
            break
        if t_enter >= 1.0 or (ix == end_ix and iy == end_iy and t_enter >= 1.0):
            break
        if t_enter >= 1.0:
            break
    if not cells or cells[-1].t_exit < 1.0:
        cells.append(GridCell(end_ix, end_iy, max(t_enter, 0.0), 1.0))
    return cells


def liang_barsky_clip(
    x0: float, y0: float, x1: float, y1: float, xmin: float, ymin: float,
    xmax: float, ymax: float,
) -> Tuple[float, float, float, float, float, float] | None:
    """Clip a segment to an axis-aligned rectangle.

    Returns ``(cx0, cy0, cx1, cy1, t0, t1)`` in original parameter space, or
    ``None`` when the segment does not intersect the rectangle.
    """
    dx = x1 - x0
    dy = y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - xmin), (dx, xmax - x0),
                 (-dy, y0 - ymin), (dy, ymax - y0)):
        if p == 0.0:
            if q < 0.0:
                return None
            continue
        r = q / p
        if p < 0.0:
            if r > t1:
                return None
            if r > t0:
                t0 = r
        else:
            if r < t0:
                return None
            if r < t1:
                t1 = r
    return x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy, t0, t1


def _polygon_area(poly: Sequence[Tuple[float, float]]) -> float:
    area = 0.0
    n = len(poly)
    for i in range(n):
        x_a, y_a = poly[i]
        x_b, y_b = poly[(i + 1) % n]
        area += x_a * y_b - x_b * y_a
    return abs(area) * 0.5


def clip_polygon_rect(
    poly: Sequence[Tuple[float, float]], xmin: float, ymin: float,
    xmax: float, ymax: float,
) -> List[Tuple[float, float]]:
    """Sutherland-Hodgman clipping of a polygon against a pixel square."""
    output = list(poly)
    edges = (
        (1.0, 0.0, -xmin, "left"),
        (-1.0, 0.0, xmax, "right"),
        (0.0, 1.0, -ymin, "bottom"),
        (0.0, -1.0, ymax, "top"),
    )
    for a, b, c, _name in edges:
        if not output:
            return []
        inputs = output
        output = []
        prev = inputs[-1]
        prev_inside = a * prev[0] + b * prev[1] + c >= -1e-12
        for point in inputs:
            inside = a * point[0] + b * point[1] + c >= -1e-12
            if inside != prev_inside:
                px, py = prev
                qx, qy = point
                denom = a * (qx - px) + b * (qy - py)
                if denom != 0.0:
                    t = -(a * px + b * py + c) / denom
                    t = min(max(t, 0.0), 1.0)
                    output.append((px + t * (qx - px), py + t * (qy - py)))
            if inside:
                output.append(point)
            prev, prev_inside = point, inside
    return output


def disk_polygon(cx: float, cy: float, radius: float, steps: int = 24) -> List[Tuple[float, float]]:
    if radius <= 0.0:
        return [(cx, cy)]
    return [
        (cx + radius * math.cos(2.0 * math.pi * k / steps),
         cy + radius * math.sin(2.0 * math.pi * k / steps))
        for k in range(steps)
    ]


def capsule_polygon(
    x0: float, y0: float, x1: float, y1: float, radius: float, steps: int = 16,
) -> List[Tuple[float, float]]:
    """Polygonise a rounded capsule around a segment for analytic clipping."""
    length = math.hypot(x1 - x0, y1 - y0)
    if length == 0.0:
        return disk_polygon(x0, y0, radius, max(steps * 2, 24))
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    nx, ny = -uy, ux
    left0 = (x0 + nx * radius, y0 + ny * radius)
    left1 = (x1 + nx * radius, y1 + ny * radius)
    right1 = (x1 - nx * radius, y1 - ny * radius)
    right0 = (x0 - nx * radius, y0 - ny * radius)
    angle_n = math.atan2(ny, nx)
    # Clockwise ring: left edge -> outward p1 semicircle -> right edge ->
    # outward p0 semicircle.  Arcs decrease the polar angle so the caps bulge
    # away from the centre-line instead of folding into the rectangle.
    poly = [left0, left1]
    for k in range(1, steps + 1):
        angle = angle_n - math.pi * k / steps
        poly.append((x1 + radius * math.cos(angle), y1 + radius * math.sin(angle)))
    poly.append(right1)
    poly.append(right0)
    for k in range(1, steps + 1):
        angle = angle_n + math.pi - math.pi * k / steps
        poly.append((x0 + radius * math.cos(angle), y0 + radius * math.sin(angle)))
    poly.append(left0)
    return poly


def capsule_cell_area_polygon(
    x0: float, y0: float, x1: float, y1: float, radius: float,
    ix: int, iy: int, steps: int = 16,
) -> float:
    """Reference analytic area: polygonise the capsule and clip to the cell."""
    poly = capsule_polygon(x0, y0, x1, y1, radius, steps=steps)
    clipped = clip_polygon_rect(poly, float(ix), float(iy), float(ix + 1), float(iy + 1))
    if len(clipped) < 3:
        return 0.0
    return min(_polygon_area(clipped), 1.0)


def strip_cell_area(
    x0: float, y0: float, x1: float, y1: float, radius: float,
    ix: int, iy: int,
) -> float:
    """Primary analytic area of the width rectangle inside a pixel cell."""
    clipped = liang_barsky_clip(
        x0, y0, x1, y1, float(ix), float(iy), float(ix + 1), float(iy + 1))
    if clipped is None:
        return 0.0
    cx0, cy0, cx1, cy1, _t0, _t1 = clipped
    seg_len = math.hypot(cx1 - cx0, cy1 - cy0)
    return min(seg_len * 2.0 * radius, 1.0)


def disk_cell_area(cx: float, cy: float, radius: float, ix: int, iy: int) -> float:
    poly = disk_polygon(cx, cy, radius, steps=24)
    clipped = clip_polygon_rect(poly, float(ix), float(iy), float(ix + 1), float(iy + 1))
    if len(clipped) < 3:
        return 0.0
    return min(_polygon_area(clipped), 1.0)


def point_in_polygon(points: np.ndarray, polygon: np.ndarray) -> np.ndarray:
    """Vectorised ray-casting containment test.

    ``points`` has shape ``(N, 2)`` and ``polygon`` has shape ``(M, 2)``.
    Returns a boolean array of length ``N``.
    """
    pts = np.atleast_2d(points).astype(np.float64)
    poly = np.asarray(polygon, dtype=np.float64)
    inside = np.zeros(len(pts), dtype=bool)
    x, y = pts[:, 0], pts[:, 1]
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        intersects = ((yi > y) != (yj > y)) & (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi)
        inside ^= intersects
        j = i
    return inside
