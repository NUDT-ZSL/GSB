"""Dome-grid rendering, brightness uniformity optimization and reports."""

from __future__ import annotations

import numpy as np

from .models import CalibrationProject, ProjectorLUT, BlendMask


def _splat_indices(lut: ProjectorLUT, gw: int, gh: int):
    """Nearest-cell (row, col) plus the valid mask for dome-grid splatting."""
    valid = lut.valid
    col = np.rint((lut.lon[valid] + np.pi) / (2.0 * np.pi) * gw).astype(int) % gw
    row = np.rint((np.pi / 2.0 - lut.lat[valid]) / (np.pi / 2.0) * (gh - 1)).astype(int)
    row = np.clip(row, 0, gh - 1)
    return row, col, valid


def render_preview(project: CalibrationProject,
                   luts: dict[str, ProjectorLUT],
                   masks: dict[str, BlendMask],
                   pattern: str = "checker"):
    """Render a fused preview onto the dome equirectangular grid.

    Each projector contributes a synthetic test pattern (its own image), the
    contributions are combined with the normalized blend weights.  Returns the
    fused RGB image plus per-channel coverage diagnostics.
    """
    gw, gh = project.grid_width, project.grid_height
    ids = [spec.projector_id for spec in project.projectors]
    # Cell-level accumulation: each projector contributes once per dome cell.
    cell_weight = np.zeros((len(ids), gh, gw), dtype=float)
    cell_color = np.zeros((len(ids), gh, gw, 3), dtype=float)
    for k, pid in enumerate(ids):
        lut, mask = luts[pid], masks[pid]
        row, col, valid = _splat_indices(lut, gw, gh)
        image = _test_pattern(lut.width, lut.height, k, len(ids), pattern)
        w = mask.weight[valid]
        np.add.at(cell_weight[k], (row, col), w)
        for c in range(3):
            np.add.at(cell_color[k, ..., c], (row, col), image[valid, c] * w)
    total_w = cell_weight.sum(axis=0)
    covered = total_w > 1e-9
    fused = np.zeros((gh, gw, 3), dtype=float)
    for k in range(len(ids)):
        share = np.where(covered, cell_weight[k] / np.where(covered, total_w, 1.0), 0.0)
        # Average color contributed by this projector into each cell.
        own_w = np.where(cell_weight[k] > 1e-9, cell_weight[k], 1.0)
        avg_color = cell_color[k] / own_w[..., None]
        fused += avg_color * share[..., None]
    fused = np.clip(fused, 0.0, 1.0)
    return fused, covered, total_w


def _test_pattern(width: int, height: int, index: int, total: int,
                  pattern: str) -> np.ndarray:
    ii, jj = np.meshgrid(np.arange(width), np.arange(height), indexing="xy")
    image = np.zeros((height, width, 3), dtype=float)
    base = np.array([
        [0.95, 0.35, 0.25],
        [0.25, 0.85, 0.45],
        [0.30, 0.50, 0.95],
        [0.90, 0.80, 0.25],
    ])[index % 4]
    if pattern == "flat":
        image[:] = base
    else:
        block = 24
        checker = ((ii // block) + (jj // block) + index) % 2
        image[..., 0] = base[0] * (0.55 + 0.45 * checker)
        image[..., 1] = base[1] * (0.55 + 0.45 * checker)
        image[..., 2] = base[2] * (0.55 + 0.45 * checker)
    return image


def brightness_report(project: CalibrationProject,
                      luts: dict[str, ProjectorLUT],
                      masks: dict[str, BlendMask]) -> dict:
    """Weight-sum uniformity measured on the dome cell grid.

    Uses the exact same coverage, distance-transform and normalization
    pipeline as :mod:`domecalib.core.blending`, so the reported weight sums
    match the produced masks instead of a lossy per-pixel splat.
    """
    from .blending import rasterize_coverage, _chamfer_distance, _feather

    gw, gh = project.grid_width, project.grid_height
    ids = [spec.projector_id for spec in project.projectors]
    params = project.blending
    coverages = {pid: rasterize_coverage(luts[pid], gw, gh) for pid in ids}
    cell_rad = (np.pi / 2.0) / max(gh - 1, 1)
    feather_cells = max(np.radians(params.feather_width_deg) / cell_rad, 1.0)

    raw = np.zeros((len(ids), gh, gw), dtype=float)
    for k, pid in enumerate(ids):
        dist = _chamfer_distance(coverages[pid])
        raw[k] = np.where(coverages[pid],
                          _feather(dist / feather_cells, params.curve, params.gamma), 0.0)
    raw_sum = raw.sum(axis=0)
    covered = raw_sum > 1e-12
    normalized = np.zeros_like(raw)
    for k in range(len(ids)):
        normalized[k] = np.where(covered, raw[k] / np.where(covered, raw_sum, 1.0), 0.0)
    contributions = normalized.sum(axis=0)
    overlap = (raw > 1e-9).sum(axis=0)

    # One multiplicative gain per projector, chosen so the fused constant
    # field approaches 1.0 in overlap cells.  Normalized weights already sum
    # to one, so the unconstrained least-squares solution stays near unity;
    # solve the normal equations in one shot and only softly bound the result.
    n_proj = len(ids)
    normal = np.zeros((n_proj, n_proj))
    rhs = np.zeros(n_proj)
    overlap_region = overlap >= 2
    for a in range(n_proj):
        for b in range(n_proj):
            normal[a, b] = float(np.sum(
                normalized[a][overlap_region] * normalized[b][overlap_region]
            ))
        rhs[a] = float(np.sum(normalized[a][overlap_region]))
    try:
        gains = np.linalg.solve(normal + 1e-9 * np.eye(n_proj), rhs)
    except np.linalg.LinAlgError:
        gains = np.ones(n_proj)
    gains = np.clip(gains, 0.25, 4.0)
    optimized = np.zeros_like(contributions)
    for k in range(len(ids)):
        optimized += normalized[k] * gains[k]

    def stats(arr, region):
        vals = arr[region]
        if vals.size == 0:
            return {"mean": 0.0, "std": 0.0, "min": 0.0, "max": 0.0}
        return {"mean": float(vals.mean()), "std": float(vals.std()),
                "min": float(vals.min()), "max": float(vals.max())}

    triple_region = overlap >= 3
    pre = stats(raw_sum, overlap_region)
    post = stats(optimized, overlap_region)
    uniformity_pre = float(pre["std"] / pre["mean"]) if pre["mean"] > 0 else 0.0
    uniformity_post = float(post["std"] / post["mean"]) if post["mean"] > 0 else 0.0

    # Seam discontinuities: largest neighbor jump between two covered overlap
    # cells (coverage edges legitimately fall to zero and are excluded).
    if overlap_region.any():
        diffs = np.abs(np.diff(optimized, axis=1))
        both_covered = overlap_region[:, :-1] & overlap_region[:, 1:]
        seam_jump = float(diffs[both_covered].max()) if np.any(both_covered) else 0.0
    else:
        seam_jump = 0.0
    return {
        "coverage_ratio": float(np.count_nonzero(covered) / covered.size),
        "overlap_cell_count": int(np.count_nonzero(overlap_region)),
        "triple_overlap_cell_count": int(np.count_nonzero(triple_region)),
        "weight_sum_pre_optimization": pre,
        "weight_sum_post_optimization": post,
        "uniformity_cv_pre": uniformity_pre,
        "uniformity_cv_post": uniformity_post,
        "max_seam_jump": seam_jump,
        "projector_gains": {pid: float(gains[k]) for k, pid in enumerate(ids)},
    }


def alignment_report(project: CalibrationProject,
                     luts: dict[str, ProjectorLUT],
                     quality: dict,
                     truth_errors: dict,
                     tolerance_rad: float) -> dict:
    """Geometry alignment statistics with per-pixel outlier classification."""
    gw, gh = project.grid_width, project.grid_height
    ids = [spec.projector_id for spec in project.projectors]
    outlier_map = np.zeros((gh, gw), dtype=np.uint8)
    class_counts = {"invalid_pixel": 0, "tps_residual": 0,
                    "distortion_inversion": 0, "hemisphere_boundary": 0}
    per_projector = {}

    for pid in ids:
        lut = luts[pid]
        row, col, valid = _splat_indices(lut, gw, gh)
        # Residual of corrected mapping relative to the raw reverse mapping is
        # the TPS contribution; large values flag residual-driven outliers.
        from .mapping import angular_distance
        tps_shift = angular_distance(lut.lon, lut.lat, lut.raw_lon, lut.raw_lat)
        tps_shift = np.where(valid, tps_shift, 0.0)
        boundary = valid & (lut.lat < np.radians(3.0))
        q = quality.get(pid)
        over = tps_shift > tolerance_rad
        for r, c, flag, bnd in zip(row, col, over[valid], boundary[valid]):
            if not flag:
                continue
            outlier_map[r, c] = 255
            if bnd:
                class_counts["hemisphere_boundary"] += 1
            elif q is not None and q.status != "ok":
                class_counts["tps_residual"] += 1
            else:
                class_counts["distortion_inversion"] += 1
        class_counts["invalid_pixel"] += int(np.count_nonzero(~valid))
        per_projector[pid] = {
            "valid_pixels": int(np.count_nonzero(valid)),
            "invalid_pixels": int(np.count_nonzero(~valid)),
            "tps_shift_rms_rad": float(np.sqrt(np.mean(tps_shift[valid] ** 2))),
            "tps_shift_max_rad": float(np.max(tps_shift[valid])),
            "outlier_pixels": int(np.count_nonzero(over)),
        }

    return {
        "tolerance_rad": tolerance_rad,
        "truth_comparison": truth_errors,
        "per_projector": per_projector,
        "error_class_counts": class_counts,
        "outlier_map_shape": list(outlier_map.shape),
        "outlier_total": int(np.count_nonzero(outlier_map)),
    }
