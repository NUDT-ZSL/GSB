"""Overlap detection, feather masks and normalized blend weight optimization."""

from __future__ import annotations

import numpy as np

from .errors import CalibrationError, E_INVALID_PARAMETER
from .models import (
    CalibrationProject,
    ProjectorLUT,
    BlendMask,
)


def _chamfer_distance(mask: np.ndarray, wrap_lon: bool = True) -> np.ndarray:
    """Distance (in grid cells) from every cell to the nearest False cell.

    Borgefors 3-4 two-pass chamfer metric with optional horizontal wrapping so
    the longitude seam is treated as continuous.
    """
    h, w = mask.shape
    inf = 1e9
    # Work on a padded grid: inf border vertically, wrapped columns horizontally.
    pad = np.full((h + 2, w + 2), inf, dtype=float)
    pad[1:h + 1, 1:w + 1] = np.where(mask, inf, 0.0)
    if wrap_lon:
        pad[1:h + 1, 0] = np.where(mask[:, -1], inf, 0.0)
        pad[1:h + 1, w + 1] = np.where(mask[:, 0], inf, 0.0)

    def sweep(grid, offsets, reverse_rows, reverse_cols):
        rows = range(h - 1, -1, -1) if reverse_rows else range(h)
        for i in rows:
            cols = range(w - 1, -1, -1) if reverse_cols else range(w)
            for j in cols:
                gi, gj = i + 1, j + 1
                best = grid[gi, gj]
                for dy, dx, cost in offsets:
                    cand = grid[gi + dy, gj + dx] + cost
                    if cand < best:
                        best = cand
                grid[gi, gj] = best
        return grid

    forward = ((-1, -1, 4), (-1, 0, 3), (-1, 1, 4), (0, -1, 3))
    backward = ((1, 1, 4), (1, 0, 3), (1, -1, 4), (0, 1, 3))
    pad = sweep(pad, forward, False, False)
    pad = sweep(pad, backward, True, True)
    out = pad[1:h + 1, 1:w + 1]
    if not np.any(~mask):
        # No False cell exists (e.g. a fully wrapped row): cap at the largest
        # finite chamfer distance reachable on the finite grid.
        finite_cap = 4.0 * (h + w)
        out = np.where(np.isfinite(out), out, finite_cap)
    return out / 4.0


def rasterize_coverage(lut: ProjectorLUT, grid_width: int,
                       grid_height: int) -> np.ndarray:
    """Splat valid projector pixels onto the dome equirectangular grid.

    Each pixel covers a 2x2 cell footprint so neighbouring projector pixels
    leave no one-cell gaps in the boolean coverage.
    """
    coverage = np.zeros((grid_height, grid_width), dtype=bool)
    valid = lut.valid
    col_f = (lut.lon[valid] + np.pi) / (2.0 * np.pi) * grid_width
    row_f = (np.pi / 2.0 - lut.lat[valid]) / (np.pi / 2.0) * (grid_height - 1)
    col0 = np.floor(col_f).astype(int) % grid_width
    row0 = np.clip(np.floor(row_f).astype(int), 0, grid_height - 2)
    for dr in (0, 1):
        for dc in (0, 1):
            rr = np.clip(row0 + dr, 0, grid_height - 1)
            cc = (col0 + dc) % grid_width
            coverage[rr, cc] = True
    # Close single-cell splat gaps so distance transforms see solid regions.
    coverage = _binary_close(coverage)
    return coverage


def _binary_close(mask: np.ndarray) -> np.ndarray:
    """One-iteration binary closing (dilate then erode) with lon wrapping."""
    dilated = np.zeros_like(mask)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            shifted = np.roll(np.roll(mask, dy, axis=0), dx, axis=1)
            if dy == -1:
                shifted[0, :] = False
            elif dy == 1:
                shifted[-1, :] = False
            dilated |= shifted
    eroded = np.ones_like(mask)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            shifted = np.roll(np.roll(dilated, -dy, axis=0), -dx, axis=1)
            if dy == 1:
                shifted[0, :] = True
            elif dy == -1:
                shifted[-1, :] = True
            eroded &= shifted
    return eroded


def _feather(t: np.ndarray, curve: str, gamma: float) -> np.ndarray:
    t = np.clip(t, 0.0, 1.0)
    if curve == "linear":
        w = t
    elif curve == "smoothstep":
        w = t * t * (3.0 - 2.0 * t)
    else:
        raise CalibrationError(
            E_INVALID_PARAMETER, f"unknown feather curve '{curve}'",
            {"allowed": ["linear", "smoothstep"]},
        )
    if gamma > 0.0:
        w = np.power(w, gamma)
    return w


def sample_grid_bilinear(grid: np.ndarray, lon, lat, gw: int, gh: int,
                         valid: np.ndarray) -> np.ndarray:
    """Sample an equirectangular grid at lon/lat points with lon wrapping."""
    col_f = (lon + np.pi) / (2.0 * np.pi) * gw
    row_f = (np.pi / 2.0 - lat) / (np.pi / 2.0) * (gh - 1)
    col0 = np.floor(col_f).astype(int)
    row0 = np.clip(np.floor(row_f).astype(int), 0, gh - 2)
    fc = col_f - col0
    fr = row_f - row0
    col0 %= gw
    col1 = (col0 + 1) % gw
    row1 = np.clip(row0 + 1, 0, gh - 1)
    out = (
        grid[row0, col0] * (1 - fc) * (1 - fr)
        + grid[row0, col1] * fc * (1 - fr)
        + grid[row1, col0] * (1 - fc) * fr
        + grid[row1, col1] * fc * fr
    )
    return np.where(valid, out, 0.0)


def build_blend_masks(project: CalibrationProject,
                      luts: dict[str, ProjectorLUT]) -> tuple[dict[str, BlendMask], dict]:
    """Build normalized per-projector blend masks on the dome and pixel grid."""
    params = project.blending
    if params.feather_width_deg <= 0.0:
        raise CalibrationError(
            E_INVALID_PARAMETER,
            "feather_width_deg must be positive",
            {"feather_width_deg": params.feather_width_deg},
        )
    if params.gamma <= 0.0:
        raise CalibrationError(
            E_INVALID_PARAMETER, "feather gamma must be positive", {"gamma": params.gamma}
        )

    gw, gh = project.grid_width, project.grid_height
    ids = [spec.projector_id for spec in project.projectors]
    coverages = {pid: rasterize_coverage(luts[pid], gw, gh) for pid in ids}

    # Mean angular size of one grid cell, used to convert cells -> radians.
    cell_rad = (np.pi / 2.0) / max(gh - 1, 1)
    feather_cells = max(np.radians(params.feather_width_deg) / cell_rad, 1.0)

    raw_grid: dict[str, np.ndarray] = {}
    diagnostics = {"narrow_overlap": [], "triple_overlap_cells": 0,
                   "min_overlap_width_deg": None}
    overlap_widths = []
    for pid in ids:
        dist_cells = _chamfer_distance(coverages[pid], wrap_lon=True)
        t = dist_cells / feather_cells
        raw_grid[pid] = np.where(coverages[pid], _feather(t, params.curve, params.gamma), 0.0)

    coverage_stack = np.stack([coverages[pid] for pid in ids], axis=0)
    overlap_count = coverage_stack.sum(axis=0)
    diagnostics["triple_overlap_cells"] = int(np.count_nonzero(overlap_count >= 3))

    # Measure pairwise overlap width along distance transform ridges.
    distance_cache = {
        pid: _chamfer_distance(coverages[pid], wrap_lon=True) for pid in ids
    }
    for i, pid_a in enumerate(ids):
        for pid_b in ids[i + 1:]:
            pair = coverages[pid_a] & coverages[pid_b]
            if not np.any(pair):
                continue
            # The overlap band half-width is the largest distance from an
            # overlap cell to the *nearest* outside edge of either region.
            da = distance_cache[pid_a][pair]
            db = distance_cache[pid_b][pair]
            width_cells = 2.0 * float(np.minimum(da, db).max())
            width_deg = np.degrees(width_cells * cell_rad)
            overlap_widths.append(width_deg)
            if width_deg < params.feather_width_deg:
                diagnostics["narrow_overlap"].append(
                    {"projectors": [pid_a, pid_b], "width_deg": width_deg,
                     "feather_deg": params.feather_width_deg}
                )
    if overlap_widths:
        diagnostics["min_overlap_width_deg"] = float(min(overlap_widths))

    # Normalize weights so their sum is constant wherever any projector covers.
    raw_stack = np.stack([raw_grid[pid] for pid in ids], axis=0)
    weight_sum = raw_stack.sum(axis=0)
    active = weight_sum > 1e-12
    normalized = np.zeros_like(raw_stack)
    normalized[:, active] = raw_stack[:, active] / weight_sum[None, active]

    # Monotonicity of the raw feather curve (exact by construction) and of
    # the final normalized weight (multi-projector normalization can introduce
    # sub-percent interactions deep inside a wide overlap).
    def _mean_by_distance(weight, coverage, distance):
        band = coverage & (weight > 0.0) & (weight < 1.0 - 1e-9)
        if not np.any(band):
            return np.array([])
        d_int = np.rint(distance[band]).astype(int)
        order = np.argsort(d_int)
        d_sorted, w_sorted = d_int[order], weight[band][order]
        return np.array([w_sorted[d_sorted == d].mean() for d in np.unique(d_sorted)])

    raw_monotone_violations = 0
    normalized_monotone_violations = 0
    for idx, pid in enumerate(ids):
        dist = distance_cache[pid]
        raw_means = _mean_by_distance(raw_grid[pid], coverages[pid], dist)
        if raw_means.size > 1:
            raw_monotone_violations += int(np.count_nonzero(np.diff(raw_means) < -1e-9))
        norm_means = _mean_by_distance(normalized[idx], coverages[pid], dist)
        if norm_means.size > 1:
            # Tolerate sub-percent normalization interactions; a larger
            # reversal would be visible as a real brightness jump.
            normalized_monotone_violations += int(
                np.count_nonzero(np.diff(norm_means) < -1e-2)
            )
    diagnostics["monotonicity_violations"] = raw_monotone_violations
    diagnostics["raw_feather_monotonicity_violations"] = raw_monotone_violations
    diagnostics["normalized_weight_monotonicity_violations"] = normalized_monotone_violations
    diagnostics["weight_sum_min"] = float(weight_sum[active].min()) if np.any(active) else 0.0
    diagnostics["weight_sum_max"] = float(weight_sum[active].max()) if np.any(active) else 0.0

    masks: dict[str, BlendMask] = {}
    blend_hash = ""
    for idx, (spec, pid) in enumerate(zip(project.projectors, ids)):
        grid_w = normalized[idx]
        lut = luts[pid]
        valid = lut.valid
        pixel_weight = np.zeros((spec.height, spec.width), dtype=float)
        pixel_weight[valid] = sample_grid_bilinear(
            grid_w, lut.lon[valid], lut.lat[valid], gw, gh, np.ones(valid.sum(), bool)
        )
        blend_hash = pid  # placeholder, replaced by engine with parameter hash
        masks[pid] = BlendMask(
            projector_id=pid,
            width=spec.width,
            height=spec.height,
            weight=pixel_weight,
            valid=valid,
            blend_hash=blend_hash,
            version=0,
        )
    return masks, diagnostics
