"""Analytic sub-pixel coverage rasterisers.

Three independent estimators share camera projection and alpha compositing:

* :func:`rasterize_strip` - primary estimator.  Every fibre/grain is turned
  into a rounded capsule; Amanatides-Woo traversal plus a radius neighbourhood
  enumerates candidate pixels, and the capsule polygon is Sutherland-Hodgman
  clipped against each pixel square for an exact covered area.
* :func:`rasterize_polygon` - independent analytic reference.  The same clip
  primitive but with a denser polygon tessellation and a bounding-box pixel
  enumeration, so the two paths cannot share a traversal bug.
* :func:`rasterize_supersample` - jittered Monte-Carlo supersampling, a
  completely different estimator used as a cross-check.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .geometry import (
    capsule_cell_area_polygon,
    capsule_polygon,
    clip_polygon_rect,
    disk_cell_area,
    segment_grid_cells,
    _polygon_area,
)
from .models import Camera, Curve, PackagingProject


@dataclass
class RasterResult:
    coverage: np.ndarray
    contributions: np.ndarray
    touched: np.ndarray
    width: int
    height: int
    method: str
    curve_ids: List[str]

    def contribution_lookup(self) -> Dict[str, List[Tuple[int, int, float, float]]]:
        lookup: Dict[str, List[Tuple[int, int, float, float]]] = {}
        for curve_id, x, y, area, alpha in self.contributions:
            lookup.setdefault(str(curve_id), []).append(
                (int(x), int(y), float(area), float(alpha)))
        return lookup

    def pixel_contributors(self, x: int, y: int) -> List[Tuple[str, float, float]]:
        rows = self.contributions[
            (self.contributions["x"] == x) & (self.contributions["y"] == y)]
        return [(str(row["curve_id"]), float(row["area"]), float(row["alpha"]))
                for row in rows]


CONTRIBUTION_DTYPE = np.dtype([
    ("curve_id", "U24"), ("x", np.int32), ("y", np.int32),
    ("area", np.float64), ("alpha", np.float64),
])


def project_curve(curve: Curve, camera: Camera) -> Tuple[np.ndarray, np.ndarray]:
    points = np.asarray(curve.points_3d, dtype=np.float64)
    xy, depth = camera.project(points)
    valid = depth > 0.0
    return xy[valid], depth[valid]


def _bounded(ix: int, iy: int, width: int, height: int) -> bool:
    return 0 <= ix < width and 0 <= iy < height


def _neighbourhood(ix: int, iy: int, radius: float,
                   min_ix: int, min_iy: int, max_ix: int, max_iy: int
                   ) -> List[Tuple[int, int]]:
    reach = max(1, int(math.ceil(radius)))
    return [
        (jx, jy)
        for jx in range(max(min_ix, ix - reach), min(max_ix, ix + reach) + 1)
        for jy in range(max(min_iy, iy - reach), min(max_iy, iy + reach) + 1)
    ]


def _segment_near_cell(x0: float, y0: float, x1: float, y1: float,
                       radius: float, ix: int, iy: int) -> bool:
    """Conservative capsule-vs-pixel rejection before exact clipping."""
    cx, cy = ix + 0.5, iy + 0.5
    vx, vy = x1 - x0, y1 - y0
    length_sq = vx * vx + vy * vy
    t = 0.0 if length_sq == 0.0 else min(
        max(((cx - x0) * vx + (cy - y0) * vy) / length_sq, 0.0), 1.0)
    px, py = x0 + t * vx, y0 + t * vy
    limit = radius + 0.7071067811865476
    return abs(cx - px) <= limit and abs(cy - py) <= limit


def analytic_cells(xy: np.ndarray, radius: float, width: int, height: int,
                   steps: int = 8) -> Dict[Tuple[int, int], float]:
    """Primary analytic per-pixel capsule area using grid traversal."""
    if len(xy) == 0:
        return {}
    min_ix = max(int(math.floor(xy[:, 0].min() - radius)), 0)
    max_ix = min(int(math.floor(xy[:, 0].max() + radius)), width - 1)
    min_iy = max(int(math.floor(xy[:, 1].min() - radius)), 0)
    max_iy = min(int(math.floor(xy[:, 1].max() + radius)), height - 1)
    areas: Dict[Tuple[int, int], float] = {}
    if len(xy) == 1:
        ix, iy = int(math.floor(xy[0, 0])), int(math.floor(xy[0, 1]))
        if _bounded(ix, iy, width, height):
            areas[(ix, iy)] = min(disk_cell_area(
                float(xy[0, 0]), float(xy[0, 1]), radius, ix, iy), 1.0)
        return areas

    for k in range(len(xy) - 1):
        x0, y0 = (float(v) for v in xy[k])
        x1, y1 = (float(v) for v in xy[k + 1])
        poly = capsule_polygon(x0, y0, x1, y1, radius, steps=steps)
        for cell in segment_grid_cells(x0, y0, x1, y1):
            for ix, iy in _neighbourhood(cell.ix, cell.iy, radius,
                                         min_ix, min_iy, max_ix, max_iy):
                if not _segment_near_cell(x0, y0, x1, y1, radius, ix, iy):
                    continue
                clipped = clip_polygon_rect(
                    poly, float(ix), float(iy), float(ix + 1), float(iy + 1))
                if len(clipped) < 3:
                    continue
                area = min(_polygon_area(clipped), 1.0)
                key = (ix, iy)
                if area > areas.get(key, 0.0):
                    areas[key] = area
    return areas


def reference_cells(xy: np.ndarray, radius: float, width: int, height: int,
                    steps: int = 24) -> Dict[Tuple[int, int], float]:
    """Independent analytic reference.

    Enumerates pixels through the segment bounding box of every crossed cell
    (a different enumeration strategy than the primary radius neighbourhood)
    and clips a densely tessellated capsule.
    """
    if len(xy) == 0:
        return {}
    min_ix = max(int(math.floor(xy[:, 0].min() - radius)), 0)
    max_ix = min(int(math.floor(xy[:, 0].max() + radius)), width - 1)
    min_iy = max(int(math.floor(xy[:, 1].min() - radius)), 0)
    max_iy = min(int(math.floor(xy[:, 1].max() + radius)), height - 1)
    areas: Dict[Tuple[int, int], float] = {}
    if len(xy) == 1:
        ix, iy = int(math.floor(xy[0, 0])), int(math.floor(xy[0, 1]))
        if _bounded(ix, iy, width, height):
            areas[(ix, iy)] = capsule_cell_area_polygon(
                float(xy[0, 0]), float(xy[0, 1]),
                float(xy[0, 0]), float(xy[0, 1]), radius, ix, iy, steps=steps)
        return areas
    for k in range(len(xy) - 1):
        x0, y0 = (float(v) for v in xy[k])
        x1, y1 = (float(v) for v in xy[k + 1])
        local_min_ix = max(int(math.floor(min(x0, x1) - radius)), min_ix)
        local_max_ix = min(int(math.floor(max(x0, x1) + radius)), max_ix)
        local_min_iy = max(int(math.floor(min(y0, y1) - radius)), min_iy)
        local_max_iy = min(int(math.floor(max(y0, y1) + radius)), max_iy)
        for ix in range(local_min_ix, local_max_ix + 1):
            for iy in range(local_min_iy, local_max_iy + 1):
                area = capsule_cell_area_polygon(
                    x0, y0, x1, y1, radius, ix, iy, steps=steps)
                if area <= 0.0:
                    continue
                key = (ix, iy)
                if area > areas.get(key, 0.0):
                    areas[key] = min(area, 1.0)
    return areas


def supersample_cells(xy: np.ndarray, radius: float, width: int, height: int,
                      samples_per_axis: int) -> Dict[Tuple[int, int], float]:
    if len(xy) < 2:
        return {}
    min_ix = max(int(math.floor(xy[:, 0].min() - radius)), 0)
    max_ix = min(int(math.floor(xy[:, 0].max() + radius)), width - 1)
    min_iy = max(int(math.floor(xy[:, 1].min() - radius)), 0)
    max_iy = min(int(math.floor(xy[:, 1].max() + radius)), height - 1)
    rng = np.random.default_rng(1729)
    jitter = rng.random((samples_per_axis, samples_per_axis, 2)) - 0.5
    offsets = (np.arange(samples_per_axis) + 0.5) / samples_per_axis
    gx, gy = np.meshgrid(offsets, offsets)
    sample_offsets = (np.stack([gx, gy], axis=-1)
                      + jitter / samples_per_axis).reshape(-1, 2)
    areas: Dict[Tuple[int, int], float] = {}
    for ix in range(min_ix, max_ix + 1):
        for iy in range(min_iy, max_iy + 1):
            sx = ix + sample_offsets[:, 0]
            sy = iy + sample_offsets[:, 1]
            inside = np.zeros(len(sx), dtype=bool)
            for k in range(len(xy) - 1):
                inside |= _points_near_segment(sx, sy, xy[k], xy[k + 1], radius)
            area = float(inside.sum() / len(sx))
            if area > 0.0:
                areas[(ix, iy)] = area
    return areas


def _points_near_segment(sx: np.ndarray, sy: np.ndarray,
                         a: np.ndarray, b: np.ndarray, radius: float) -> np.ndarray:
    vx, vy = b[0] - a[0], b[1] - a[1]
    length_sq = vx * vx + vy * vy
    t = np.zeros_like(sx) if length_sq == 0.0 else np.clip(
        ((sx - a[0]) * vx + (sy - a[1]) * vy) / length_sq, 0.0, 1.0)
    px = a[0] + t * vx
    py = a[1] + t * vy
    return (sx - px) ** 2 + (sy - py) ** 2 <= radius * radius


def rasterize_strip(project: PackagingProject, camera: Camera,
                    curves: Optional[Sequence[Curve]] = None) -> RasterResult:
    return _rasterize(project, camera, curves,
                      lambda xy, r, w, h: analytic_cells(xy, r, w, h, steps=8),
                      "analytic_capsule")


def rasterize_polygon(project: PackagingProject, camera: Camera,
                      curves: Optional[Sequence[Curve]] = None) -> RasterResult:
    return _rasterize(project, camera, curves,
                      lambda xy, r, w, h: reference_cells(xy, r, w, h, steps=24),
                      "analytic_reference")


def rasterize_supersample(project: PackagingProject, camera: Camera,
                          curves: Optional[Sequence[Curve]] = None,
                          samples_per_axis: int = 12) -> RasterResult:
    if isinstance(curves, int):
        samples_per_axis = curves
        curves = None
    return _rasterize(
        project, camera, curves,
        lambda xy, r, w, h: supersample_cells(xy, r, w, h, samples_per_axis),
        f"supersample_{samples_per_axis}x{samples_per_axis}")


def _rasterize(project: PackagingProject, camera: Camera,
               curves: Optional[Sequence[Curve]], cell_fn, method: str) -> RasterResult:
    width, height = camera.width_px, camera.height_px
    targets = list(curves) if curves is not None else project.valid_curves()
    coverage = np.zeros((height, width), dtype=np.float64)
    touched = np.zeros((height, width), dtype=bool)
    records: List[Tuple] = []
    curve_ids: List[str] = []

    for curve in targets:
        xy, _depth = project_curve(curve, camera)
        cells = cell_fn(xy, curve.radius_px, width, height)
        if not cells:
            continue
        curve_ids.append(curve.curve_id)
        for (ix, iy), area in cells.items():
            area = min(float(area), 1.0)
            alpha = area * curve.opacity
            coverage[iy, ix] = min(1.0, coverage[iy, ix] + alpha)
            touched[iy, ix] = True
            records.append((curve.curve_id, ix, iy, area, alpha))

    contributions = np.array(records, dtype=CONTRIBUTION_DTYPE)
    return RasterResult(
        coverage=coverage, contributions=contributions, touched=touched,
        width=width, height=height, method=method, curve_ids=curve_ids)
