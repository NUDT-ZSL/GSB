"""Local parameter adjustments and incremental coverage recomputation."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .models import Curve, PackagingProject
from .rasterizer import RasterResult, analytic_cells, project_curve, rasterize_strip


@dataclass
class CurveAdjustment:
    """A requested change to a single existing curve."""

    curve_id: str
    radius_px: Optional[float] = None
    opacity: Optional[float] = None


@dataclass
class DensityAdjustment:
    """Add or remove fibres/grains inside an outline-coordinate region."""

    kind: str
    material: str
    surface: str
    region: Tuple[float, float, float, float]
    add: int = 0
    remove: int = 0


@dataclass
class IncrementalResult:
    changed_curve_ids: List[str]
    affected_pixels: List[Tuple[int, int]]
    removed_pixels: List[Tuple[int, int]]
    added_pixels: List[Tuple[int, int]]
    recomputed_contribution_rows: int
    elapsed_ms: float
    full_recompute_elapsed_ms: float
    equal_to_full: bool
    untouched_max_error: float
    thumbnail_block_range: List[Tuple[int, int]]
    note: str = ""


def apply_adjustments(project: PackagingProject,
                      curve_changes: Sequence[CurveAdjustment],
                      density_changes: Sequence[DensityAdjustment]) -> Tuple[PackagingProject, List[str]]:
    """Return a version-bumped project copy and the list of changed curve ids."""
    changed: List[str] = []
    curves: List[Curve] = [
        Curve(**{**curve.__dict__}) for curve in project.curves
    ]
    by_id = {curve.curve_id: curve for curve in curves}
    for change in curve_changes:
        curve = by_id.get(change.curve_id)
        if curve is None:
            continue
        if change.radius_px is not None:
            curve.radius_px = float(change.radius_px)
        if change.opacity is not None:
            curve.opacity = float(change.opacity)
        changed.append(change.curve_id)

    next_serial = 1 + max(
        (int(curve.curve_id.split("-")[-1]) for curve in curves
         if curve.curve_id.split("-")[-1].isdigit()),
        default=0,
    )
    rng = np.random.default_rng(next_serial * 7919 + project.seed)
    for density in density_changes:
        candidates = [
            curve for curve in curves
            if curve.kind == density.kind and curve.material == density.material
            and curve.surface == density.surface
            and _inside(curve.start_2d, density.region)
        ]
        for curve in candidates[:density.remove]:
            if curve.curve_id not in changed:
                changed.append(curve.curve_id)
        removed_ids = {curve.curve_id for curve in candidates[:density.remove]}
        curves = [curve for curve in curves if curve.curve_id not in removed_ids]

        from .generator import _build_polyline, _polyline_length
        for _ in range(density.add):
            angle = float(rng.uniform(0.0, 2.0 * np.pi))
            tangent = (np.cos(angle), np.sin(angle))
            if density.kind == "grain":
                length = float(rng.uniform(0.012, 0.045))
                radius = float(rng.uniform(0.2, 0.45))
            else:
                length = float(rng.uniform(0.15, 0.5))
                radius = float(rng.uniform(0.35, 0.85))
            u = float(rng.uniform(density.region[0], density.region[2]))
            v = float(rng.uniform(density.region[1], density.region[3]))
            start = (u, v)
            points = _build_polyline(start, tangent, length, density.surface)
            curve_id = f"AD-{next_serial:04d}"
            next_serial += 1
            curves.append(Curve(
                curve_id=curve_id,
                kind=density.kind,
                material=density.material,
                start_2d=start,
                tangent_2d=(float(tangent[0]), float(tangent[1])),
                declared_length=_polyline_length(points),
                radius_px=radius,
                opacity=float(rng.uniform(0.35, 0.9)),
                surface=density.surface,
                points_3d=points,
            ))
            changed.append(curve_id)

    updated = PackagingProject(
        project_id=project.project_id,
        seed=project.seed,
        version=_bump_version(project.version),
        outline_bounds=project.outline_bounds,
        body=project.body,
        cap=project.cap,
        die_lines=project.die_lines,
        labels=project.labels,
        lights=project.lights,
        camera=project.camera,
        curves=curves,
        material_of_surface=project.material_of_surface,
        issues=list(project.issues),
    )
    return updated, changed


def _inside(point: Tuple[float, float], region: Tuple[float, float, float, float]) -> bool:
    u0, v0, u1, v1 = region
    return u0 <= point[0] <= u1 and v0 <= point[1] <= v1


def _bump_version(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def incremental_recompute(project: PackagingProject, previous: RasterResult,
                          changed_curve_ids: Sequence[str],
                          thumb_factor_x: int = 4,
                          thumb_factor_y: int = 4) -> Tuple[RasterResult, IncrementalResult]:
    """Recompute only pixels touched by changed curves.

    The previous per-curve contributions for affected pixels are removed, the
    changed curves are re-rasterised, and the remaining curves are re-composited
    on those pixels.  Every other pixel keeps its previous value bit for bit.
    """
    started = time.perf_counter()
    changed_set = set(changed_curve_ids)
    changed_curves = [
        curve for curve in project.valid_curves() if curve.curve_id in changed_set
    ]

    old_rows = previous.contributions[
        np.isin(previous.contributions["curve_id"], list(changed_set))
    ] if len(previous.contributions) else previous.contributions
    affected_set = {(int(row["x"]), int(row["y"])) for row in old_rows}

    record_dtype = previous.contributions.dtype
    new_records: List[Tuple] = []
    new_pixels: set[Tuple[int, int]] = set()
    for curve in changed_curves:
        xy, _depth = project_curve(curve, project.camera)
        cells = analytic_cells(xy, curve.radius_px, previous.width, previous.height)
        for (ix, iy), area in cells.items():
            area = min(float(area), 1.0)
            alpha = area * curve.opacity
            new_records.append((curve.curve_id, ix, iy, area, alpha))
            new_pixels.add((ix, iy))
            affected_set.add((ix, iy))

    kept_rows = previous.contributions[
        ~np.isin(previous.contributions["curve_id"], list(changed_set))
    ] if len(previous.contributions) else previous.contributions

    coverage = previous.coverage.copy()
    if affected_set:
        xs = np.array([pixel[0] for pixel in affected_set], dtype=np.int32)
        ys = np.array([pixel[1] for pixel in affected_set], dtype=np.int32)
        coverage[ys, xs] = 0.0
        merged = np.concatenate([kept_rows, np.array(new_records, dtype=record_dtype)])
        sel = np.zeros(len(merged), dtype=bool)
        for x, y in affected_set:
            sel |= (merged["x"] == x) & (merged["y"] == y)
        for row in merged[sel]:
            x, y = int(row["x"]), int(row["y"])
            coverage[y, x] = min(1.0, coverage[y, x] + float(row["alpha"]))
        contributions = merged
    else:
        contributions = kept_rows

    touched = coverage > 0.0
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    full_started = time.perf_counter()
    full = rasterize_strip(project, project.camera)
    full_elapsed_ms = (time.perf_counter() - full_started) * 1000.0

    affected = sorted(affected_set)
    if affected:
        ax = np.array([pixel[0] for pixel in affected])
        ay = np.array([pixel[1] for pixel in affected])
        equal = bool(np.allclose(coverage[ay, ax], full.coverage[ay, ax], atol=1e-12))
        untouched = np.ones_like(coverage, dtype=bool)
        untouched[ay, ax] = False
        untouched_error = float(np.abs(coverage - full.coverage)[untouched].max())
        equal = equal and bool(np.allclose(coverage[untouched], full.coverage[untouched],
                                           atol=1e-12))
    else:
        equal = bool(np.allclose(coverage, full.coverage, atol=1e-12))
        untouched_error = float(np.abs(coverage - full.coverage).max())

    thumb_blocks = sorted({
        (x // thumb_factor_x, y // thumb_factor_y) for x, y in affected
    })
    result = RasterResult(
        coverage=coverage,
        contributions=contributions,
        touched=touched,
        width=previous.width,
        height=previous.height,
        method=previous.method,
        curve_ids=sorted(set(previous.curve_ids) | changed_set),
    )
    report = IncrementalResult(
        changed_curve_ids=sorted(changed_set),
        affected_pixels=affected,
        removed_pixels=sorted(affected_set - new_pixels),
        added_pixels=sorted(new_pixels - {(int(r["x"]), int(r["y"])) for r in old_rows}),
        recomputed_contribution_rows=len(new_records),
        elapsed_ms=elapsed_ms,
        full_recompute_elapsed_ms=full_elapsed_ms,
        equal_to_full=equal,
        untouched_max_error=untouched_error,
        thumbnail_block_range=thumb_blocks,
    )
    return result, report


def report_to_dict(report: IncrementalResult) -> Dict:
    return {
        "changed_curve_ids": report.changed_curve_ids,
        "affected_pixel_count": len(report.affected_pixels),
        "affected_pixels": report.affected_pixels[:300],
        "removed_pixels": report.removed_pixels[:200],
        "added_pixels": report.added_pixels[:200],
        "recomputed_contribution_rows": report.recomputed_contribution_rows,
        "elapsed_ms": report.elapsed_ms,
        "full_recompute_elapsed_ms": report.full_recompute_elapsed_ms,
        "equal_to_full": report.equal_to_full,
        "untouched_max_error": report.untouched_max_error,
        "thumbnail_block_range": report.thumbnail_block_range,
        "note": report.note,
    }
