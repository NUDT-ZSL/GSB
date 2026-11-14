"""Contour extraction (marching squares) and nearest-contour queries.

Coordinate convention
---------------------
* Grid sample ``(ix, iy)`` sits at world
  ``(min_x + ix*cell_x, min_y + iy*cell_y)``.
* Rows are row-major: ``iy == 0`` is the top row (smallest world y).
* World ``y`` grows downwards, matching PNG pixel ``y``.  The preview and the
  query results therefore share one mapping:
  ``px = (x - min_x) / (max_x - min_x) * canvas_w`` and likewise for ``py``.
* All distances in the result JSON are world-space Euclidean distances.
"""

from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass

from .scene import Scene

EPS = 1e-12
Point = tuple[float, float]
Segment = tuple[Point, Point]


@dataclass(frozen=True)
class LevelContours:
    level: float
    segments: tuple[Segment, ...]

    @property
    def empty(self) -> bool:
        return not self.segments


@dataclass(frozen=True)
class QueryHit:
    status: str                    # "hit" | "miss" | "skipped"
    level: float | None = None
    nearest: Point | None = None
    distance: float | None = None
    reason: str | None = None


def _lerp(a: Point, b: Point, t: float) -> Point:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _crossing(a_val: float, b_val: float, a_pt: Point, b_pt: Point, level: float) -> Point | None:
    """Point where the edge a-b crosses ``level``, or None if it does not.

    Edges lying entirely on the level are ignored (both endpoints equal), so a
    flat contour does not produce an infinite line.
    """
    da = a_val - level
    db = b_val - level
    if da == 0.0 and db == 0.0:
        return None
    if da * db > 0.0:
        return None
    if da == 0.0:
        return a_pt
    if db == 0.0:
        return b_pt
    t = da / (da - db)  # in (0, 1)
    return _lerp(a_pt, b_pt, t)


def _segments_in_cell(
    corners: list[float],
    points: list[Point],
    level: float,
) -> list[Segment]:
    """Marching squares for one cell.

    ``corners`` / ``points`` are ordered c0=(ix,iy), c1=(ix+1,iy),
    c2=(ix+1,iy+1), c3=(ix,iy+1); edges are e0=c0-c1, e1=c1-c2, e2=c3-c2,
    e3=c0-c3.
    """
    edges = ((0, 1, 0), (1, 2, 1), (3, 2, 2), (0, 3, 3))  # a, b, edge id
    crossings: list[tuple[int, Point]] = []
    for a, b, edge_id in edges:
        pt = _crossing(corners[a], corners[b], points[a], points[b], level)
        if pt is not None:
            # Skip duplicates produced when two edges meet exactly on the level.
            if not any(math.dist(pt, existing) <= EPS for _, existing in crossings):
                crossings.append((edge_id, pt))

    if len(crossings) == 0:
        return []
    if len(crossings) == 2:
        return [(crossings[0][1], crossings[1][1])]
    if len(crossings) == 4:
        center = sum(corners) / 4.0
        ids = {edge_id: pt for edge_id, pt in crossings}
        if set(ids) != {0, 1, 2, 3}:
            # Degenerate corner geometry; fall through to the generic pairing.
            pass
        else:
            # Saddle cell: the middle value decides which diagonal region is
            # connected.  Isolate the corners of the opposite class and pair
            # the two edges that touch each isolated corner.
            # corner -> its two incident edge ids, ordered clockwise.
            incident = {0: (0, 3), 1: (0, 1), 2: (1, 2), 3: (2, 3)}
            if center >= level:
                isolated = [i for i, v in enumerate(corners) if v < level]
            else:
                isolated = [i for i, v in enumerate(corners) if v >= level]
            if len(isolated) == 2:
                result = []
                for corner in isolated:
                    e_a, e_b = incident[corner]
                    result.append((ids[e_a], ids[e_b]))
                return result
    # Odd / degenerate configurations (typically a corner exactly on the level):
    # pair the crossings in angular order around the cell centre.
    cx = sum(p[0] for p in points) / 4.0
    cy = sum(p[1] for p in points) / 4.0
    ordered = sorted(crossings, key=lambda item: math.atan2(item[1][1] - cy, item[1][0] - cx))
    result = []
    for i in range(0, len(ordered) - 1, 2):
        result.append((ordered[i][1], ordered[i + 1][1]))
    return result


def extract_level(scene: Scene, level: float) -> LevelContours:
    grid = scene.grid
    width, height = grid.width, grid.height
    world = scene.world
    cx, cy = scene.cell_x, scene.cell_y

    def point(ix: int, iy: int) -> Point:
        return (world.min_x + ix * cx, world.min_y + iy * cy)

    segments: list[Segment] = []
    if width >= 2 and height >= 2:
        for iy in range(height - 1):
            for ix in range(width - 1):
                v = [
                    grid.value(ix, iy),
                    grid.value(ix + 1, iy),
                    grid.value(ix + 1, iy + 1),
                    grid.value(ix, iy + 1),
                ]
                if any(item is None for item in v):
                    continue
                values = [float(item) for item in v]
                lo, hi = min(values), max(values)
                if level < lo or level > hi:
                    continue
                pts = [point(ix, iy), point(ix + 1, iy), point(ix + 1, iy + 1), point(ix, iy + 1)]
                segments.extend(_segments_in_cell(values, pts, level))
    elif width >= 2 and height == 1:
        # A one-row profile: every crossing edge yields a located contour point.
        for ix in range(width - 1):
            a, b = grid.value(ix, 0), grid.value(ix + 1, 0)
            if a is None or b is None:
                continue
            pa, pb = point(ix, 0), point(ix + 1, 0)
            pt = _crossing(a, b, pa, pb, level)
            if pt is not None:
                segments.append((pt, pt))
    elif width == 1 and height >= 2:
        for iy in range(height - 1):
            a, b = grid.value(0, iy), grid.value(0, iy + 1)
            if a is None or b is None:
                continue
            pa, pb = point(0, iy), point(0, iy + 1)
            pt = _crossing(a, b, pa, pb, level)
            if pt is not None:
                segments.append((pt, pt))
    return LevelContours(level=level, segments=tuple(segments))


def extract_all(scene: Scene) -> dict[float, LevelContours]:
    """Extract every configured level.  Levels outside the data range yield an
    empty contour instead of an error."""
    return {level: extract_level(scene, level) for level in scene.levels}


def _closest_on_segment(p: Point, a: Point, b: Point) -> tuple[Point, float]:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= EPS:
        q = a
    else:
        t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / length_sq
        t = min(1.0, max(0.0, t))
        q = (ax + t * dx, ay + t * dy)
    return q, math.dist(p, q)


def locate(scene: Scene, contours: dict[float, LevelContours]) -> list[QueryHit]:
    results: list[QueryHit] = []
    for index, query in enumerate(scene.queries):
        if query is None:
            results.append(QueryHit(status="skipped", reason="坐标缺失或不是有限数字"))
            continue
        wx, wy = query
        # A point exactly on the world border is still inside (closed bounds).
        if not scene.in_bounds(wx, wy):
            results.append(
                QueryHit(
                    status="skipped",
                    reason=(
                        f"查询点 ({wx:g}, {wy:g}) 超出世界范围 "
                        f"x[{scene.world.min_x:g}, {scene.world.max_x:g}] "
                        f"y[{scene.world.min_y:g}, {scene.world.max_y:g}]"
                    ),
                )
            )
            continue
        best: tuple[float, float, Point] | None = None
        for level in scene.levels:
            for a, b in contours[level].segments:
                q, dist = _closest_on_segment((wx, wy), a, b)
                if best is None or dist < best[0] - EPS:
                    best = (dist, level, q)
        if best is None:
            results.append(
                QueryHit(status="miss", reason="没有可用的等值线（所有级别均为空）")
            )
        else:
            dist, level, nearest = best
            results.append(
                QueryHit(status="hit", level=level, nearest=nearest, distance=dist)
            )
    return results


def level_colors(count: int) -> list[tuple[int, int, int]]:
    """Distinct, reasonably saturated colours, one per level."""
    palette = [
        (214, 39, 40),    # red
        (31, 119, 180),   # blue
        (44, 160, 44),    # green
        (255, 127, 14),   # orange
        (148, 103, 189),  # purple
        (140, 86, 75),    # brown
        (227, 119, 194),  # pink
        (23, 190, 207),   # cyan
        (188, 189, 34),   # olive
    ]
    colors = []
    for index in range(count):
        if index < len(palette):
            colors.append(palette[index])
        else:
            hue = (index * 0.61803398875) % 1.0
            r, g, b = colorsys.hsv_to_rgb(hue, 0.65, 0.75)
            colors.append((int(r * 255), int(g * 255), int(b * 255)))
    return colors
