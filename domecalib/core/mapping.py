"""Reverse mapping (projector pixel -> dome lon/lat) and TPS residual field."""

from __future__ import annotations

import numpy as np

from .errors import (
    CalibrationError,
    E_INSUFFICIENT_POINTS,
    E_NEAR_COLLINEAR,
    E_NO_DOME_INTERSECTION,
)
from .geometry import (
    make_basis,
    pixel_sensor_grid,
    undistort_radial,
    validate_distortion,
    ray_sphere,
    world_to_dome_local,
    local_to_latlon,
)
from .models import (
    DomeSpec,
    ProjectorSpec,
    ControlPointSet,
    ProjectorLUT,
    CorrectionQuality,
)

MIN_CONTROL_POINTS = 8
COLLINEARITY_RCOND = 1e-6
# Correction is clipped to avoid runaway coordinates from a degraded solve.
CORRECTION_CLIP_RAD = 0.05


def angular_distance(lon_a, lat_a, lon_b, lat_b) -> np.ndarray:
    """Great-circle angular distance (radians), robust to the lon seam."""
    dlat = lat_a - lat_b
    dlon = (lon_a - lon_b + np.pi) % (2.0 * np.pi) - np.pi
    hav = np.sin(dlat / 2.0) ** 2 + np.cos(lat_a) * np.cos(lat_b) * np.sin(dlon / 2.0) ** 2
    return 2.0 * np.arcsin(np.sqrt(np.clip(hav, 0.0, 1.0)))


def reverse_map_pixels(spec: ProjectorSpec, dome: DomeSpec,
                       undistort_iter: int = 30):
    """Reverse-map every projector pixel onto the dome.

    Returns ``(lon, lat, valid)`` each shaped ``(height, width)``.  Pixels
    outside the valid view frustum (no positive sphere intersection or below
    the hemisphere) are masked invalid and carry NaN coordinates.
    """
    sx, sy, tan_h, tan_v = pixel_sensor_grid(
        spec.width, spec.height, spec.fov_h_deg
    )
    sensor_radius = np.sqrt(tan_h ** 2 + tan_v ** 2)
    validate_distortion(spec.k1, spec.k2, sensor_radius)

    xu, yu = undistort_radial(sx, sy, spec.k1, spec.k2,
                              max_iter=undistort_iter)
    basis = make_basis(np.asarray(spec.forward), np.asarray(spec.up_hint))
    dirs = np.stack([xu, yu, np.ones_like(xu)], axis=-1) @ basis.T
    dirs /= np.linalg.norm(dirs, axis=-1, keepdims=True)

    points, hit = ray_sphere(
        np.asarray(spec.position), dirs, np.asarray(dome.center), dome.radius
    )
    local = world_to_dome_local(points, np.asarray(dome.center), dome.tilt)
    upper = local[..., 2] >= -1e-9
    valid = hit & upper
    if not np.any(valid):
        raise CalibrationError(
            E_NO_DOME_INTERSECTION,
            "projector frustum has no intersection with the upper dome",
            {"projector_id": spec.projector_id},
        )
    lon, lat = local_to_latlon(local, dome.radius)
    lon = np.where(valid, lon, np.nan)
    lat = np.where(valid, lat, np.nan)
    return lon, lat, valid


def ground_truth_lut(spec: ProjectorSpec, dome: DomeSpec):
    """High-iteration reference reverse mapping used by acceptance tests."""
    return reverse_map_pixels(spec, dome, undistort_iter=120)


class TPSField:
    """Thin-plate-spline 2D correction field fitted in (lon, lat) space."""

    def __init__(self, src_xy, target_xy, condition_number, regularization,
                 status, warnings):
        src_xy = np.asarray(src_xy, dtype=float)
        target_xy = np.asarray(target_xy, dtype=float)
        n = src_xy.shape[0]
        self.src = src_xy
        self.centroid = src_xy.mean(axis=0)
        self.scale = max(np.ptp(src_xy, axis=0).max(), 1e-6)
        xs = (src_xy - self.centroid) / self.scale
        diff = xs[:, None, :] - xs[None, :, :]
        r2 = np.sum(diff ** 2, axis=-1)
        with np.errstate(divide="ignore", invalid="ignore"):
            kernel = np.where(r2 > 0.0, r2 * np.log(np.maximum(r2, 1e-300)), 0.0)
        ones = np.ones((n, 1))
        top = np.concatenate([ones, xs], axis=1)
        lhs = np.block([
            [kernel + regularization * np.eye(n), top],
            [top.T, np.zeros((3, 3))],
        ])
        rhs = np.zeros((n + 3, 2))
        rhs[:n] = target_xy - src_xy
        # SVD solve: never produces divergent coordinates, even if singular.
        u, svals, vh = np.linalg.svd(lhs)
        cutoff = svals[0] * max(lhs.shape) * np.finfo(float).eps
        inv_s = np.where(svals > cutoff, 1.0 / np.where(svals > cutoff, svals, 1.0), 0.0)
        # Pseudoinverse solution: x = V diag(1/s) U^T b.
        coef = vh.T @ (inv_s[:, None] * (u.T @ rhs))
        self.weights = coef[:n]
        self.affine = coef[n:]
        self.condition_number = condition_number
        self.regularization = regularization
        self.status = status
        self.warnings = warnings

    def evaluate(self, xy):
        xy = np.asarray(xy, dtype=float)
        single = xy.ndim == 1
        if single:
            xy = xy[None, :]
        xs = (xy - self.centroid) / self.scale
        src_s = (self.src - self.centroid) / self.scale
        diff = xs[:, None, :] - src_s[None, :, :]
        r2 = np.sum(diff ** 2, axis=-1)
        with np.errstate(divide="ignore", invalid="ignore"):
            kernel = np.where(r2 > 0.0, r2 * np.log(np.maximum(r2, 1e-300)), 0.0)
        corr = kernel @ self.weights
        corr += np.concatenate([np.ones((xs.shape[0], 1)), xs], axis=1) @ self.affine
        corr = np.clip(corr, -CORRECTION_CLIP_RAD, CORRECTION_CLIP_RAD)
        out = xy + corr
        if single:
            return out[0]
        return out


def _collinearity_metric(points: np.ndarray) -> float:
    """Return a 0..1 rank-2 spread metric; near 1 means well spread in 2D."""
    centered = points - points.mean(axis=0)
    singular = np.linalg.svd(centered, compute_uv=False)
    if singular[0] <= 0.0:
        return 0.0
    return float(singular[1] / singular[0])


def fit_tps(points: ControlPointSet) -> tuple[TPSField, CorrectionQuality]:
    """Fit the residual correction field with diagnostics and safe fallbacks."""
    warnings: list[str] = []
    n = points.count()
    if n < MIN_CONTROL_POINTS:
        raise CalibrationError(
            E_INSUFFICIENT_POINTS,
            f"need at least {MIN_CONTROL_POINTS} control points, got {n}",
            {"projector_id": points.projector_id, "count": n,
             "minimum": MIN_CONTROL_POINTS},
        )

    src = np.column_stack([points.observed_lon, points.observed_lat])
    tgt = np.column_stack([points.ideal_lon, points.ideal_lat])
    spread = _collinearity_metric(src)
    if spread < COLLINEARITY_RCOND:
        raise CalibrationError(
            E_NEAR_COLLINEAR,
            "control points are near collinear and cannot define a 2D TPS",
            {"projector_id": points.projector_id, "spread_metric": spread},
        )

    # Mild collinearity -> stronger regularization (degraded but bounded).
    regularization = 1e-6
    status = "ok"
    if spread < 1e-3:
        regularization = 1e-3
        status = "degraded"
        warnings.append(
            f"control points poorly spread (metric={spread:.2e}); "
            "regularization increased"
        )

    field = TPSField(src, tgt, condition_number=1.0 / max(spread, 1e-12),
                     regularization=regularization, status=status,
                     warnings=list(warnings))

    fitted = field.evaluate(src)
    resid = np.sqrt(np.sum((fitted - tgt) ** 2, axis=1))
    rms = float(np.sqrt(np.mean(resid ** 2)))
    max_res = float(np.max(resid))

    # Leave-one-out cross validation on a constant-offset fallback is exact;
    # for TPS we refit n times on n-1 points (n is small).
    cv_errs = []
    for k in range(n):
        keep = np.ones(n, dtype=bool)
        keep[k] = False
        sub = ControlPointSet(
            projector_id=points.projector_id,
            ideal_lon=points.ideal_lon[keep],
            ideal_lat=points.ideal_lat[keep],
            observed_lon=points.observed_lon[keep],
            observed_lat=points.observed_lat[keep],
            noise_scale=points.noise_scale,
        )
        if sub.count() < MIN_CONTROL_POINTS:
            continue
        sub_spread = _collinearity_metric(
            np.column_stack([sub.observed_lon, sub.observed_lat])
        )
        if sub_spread < COLLINEARITY_RCOND:
            continue
        sub_field = TPSField(
            np.column_stack([sub.observed_lon, sub.observed_lat]),
            np.column_stack([sub.ideal_lon, sub.ideal_lat]),
            condition_number=1.0,
            regularization=regularization,
            status="ok", warnings=[],
        )
        pred = sub_field.evaluate(src[k][None, :])[0]
        cv_errs.append(np.linalg.norm(pred - tgt[k]))
    loocv_rms = float(np.sqrt(np.mean(np.square(cv_errs)))) if cv_errs else float("nan")

    # Residual imbalance: flag degradation only when the cross-validation error
    # is large on an absolute scale (well above the control-point noise floor)
    # and clearly exceeds the in-sample fit.  A near-zero fit RMS with a small
    # LOOCV RMS is normal for an interpolating TPS through noisy points.
    # The natural scale of the field being fitted is the RMS observed-to-ideal
    # displacement (warp + noise).  LOOCV at this scale means the TPS is
    # interpolating the data faithfully rather than exposing a degenerate fit.
    displacement = np.sqrt(
        (points.observed_lon - points.ideal_lon) ** 2
        + (points.observed_lat - points.ideal_lat) ** 2
    )
    field_scale = max(float(np.sqrt(np.mean(displacement ** 2))), 1e-12)
    if (
        np.isfinite(loocv_rms)
        and loocv_rms > 5.0 * max(rms, field_scale)
        and loocv_rms > 2.0 * field_scale
    ):
        status = "degraded"
        warnings.append(
            f"LOOCV RMS {loocv_rms:.2e} far exceeds fit RMS {rms:.2e}; "
            "residual distribution is uneven"
        )

    quality = CorrectionQuality(
        projector_id=points.projector_id,
        status=status,
        condition_number=field.condition_number,
        regularization=regularization,
        rms_residual_rad=rms,
        max_residual_rad=max_res,
        loocv_rms_rad=loocv_rms,
        warnings=warnings,
    )
    return field, quality


def build_projector_lut(spec: ProjectorSpec, dome: DomeSpec,
                        points: ControlPointSet | None,
                        spec_hash: str, points_hash: str,
                        blend_hash: str, version: int) -> tuple[ProjectorLUT, CorrectionQuality | None]:
    """Full per-projector pipeline: reverse map + optional TPS correction."""
    raw_lon, raw_lat, valid = reverse_map_pixels(spec, dome)
    lon = raw_lon.copy()
    lat = raw_lat.copy()
    quality = None
    if points is not None and points.count() >= MIN_CONTROL_POINTS:
        field, quality = fit_tps(points)
        coords = np.column_stack([raw_lon[valid], raw_lat[valid]])
        corrected = field.evaluate(coords)
        lon[valid] = corrected[:, 0]
        lat[valid] = corrected[:, 1]
        # Keep latitude inside the hemisphere and longitude in [-pi, pi].
        lat = np.where(valid, np.clip(lat, 0.0, np.pi / 2.0), np.nan)
        lon = np.where(valid, (lon + np.pi) % (2.0 * np.pi) - np.pi, np.nan)
    elif points is not None:
        raise CalibrationError(
            E_INSUFFICIENT_POINTS,
            f"need at least {MIN_CONTROL_POINTS} control points, got {points.count()}",
            {"projector_id": spec.projector_id, "count": points.count()},
        )

    lut = ProjectorLUT(
        projector_id=spec.projector_id,
        width=spec.width,
        height=spec.height,
        lon=lon,
        lat=lat,
        raw_lon=raw_lon,
        raw_lat=raw_lat,
        valid=valid,
        spec_hash=spec_hash,
        points_hash=points_hash,
        blend_hash=blend_hash,
        version=version,
    )
    return lut, quality


def compare_lut_with_truth(lut: ProjectorLUT, spec: ProjectorSpec,
                           dome: DomeSpec) -> dict:
    """Pixel-wise comparison against the high-iteration ground truth."""
    t_lon, t_lat, t_valid = ground_truth_lut(spec, dome)
    # The acceptance criterion concerns the pure reverse mapping (lens
    # inversion + ray intersection), which is stored on the raw_* channels
    # before any TPS residual correction is applied.
    both = lut.valid & t_valid
    err = angular_distance(lut.raw_lon, lut.raw_lat, t_lon, t_lat)
    err_valid = err[both]
    return {
        "projector_id": spec.projector_id,
        "valid_pixels": int(np.count_nonzero(both)),
        "max_angular_error_rad": float(np.max(err_valid)),
        "rms_angular_error_rad": float(np.sqrt(np.mean(err_valid ** 2))),
        "invalid_agreement": bool(np.array_equal(lut.valid, t_valid)),
    }
