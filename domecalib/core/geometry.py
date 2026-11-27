"""Geometry primitives shared by the sample generator and the calibration kernel.

Coordinate frames
-----------------
* World frame: right-handed Cartesian coordinates.
* Dome local frame: the hemisphere is the upper half (``z_local >= 0``) of a
  sphere centered at the origin.  The dome is placed in the world by a
  translation ``center`` and a tilt rotation about the world x axis.
* Projector camera frame: ``+z`` is the projection direction, ``+x`` points
  right and ``+y`` up; it is a right-handed frame.

All routines are vectorised with NumPy so an entire projector frame can be
processed in one call.
"""

from __future__ import annotations

import numpy as np

from .errors import CalibrationError, E_DISTORTION_OUT_OF_RANGE, E_INVALID_PARAMETER

# Lens distortion coefficients are only accepted inside this conservative box.
K1_LIMIT = 0.5
K2_LIMIT = 0.5


def rot_x(angle: float) -> np.ndarray:
    """3x3 rotation matrix for a rotation of ``angle`` radians about x."""
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]], dtype=float)


def unit(vec: np.ndarray) -> np.ndarray:
    vec = np.asarray(vec, dtype=float)
    norm = np.linalg.norm(vec)
    if norm < 1e-12:
        raise CalibrationError(
            E_INVALID_PARAMETER, "zero length direction vector", {"vector": vec.tolist()}
        )
    return vec / norm


def make_basis(forward: np.ndarray, up_hint: np.ndarray) -> np.ndarray:
    """Return the 3x3 camera basis whose columns are (right, up, forward)."""
    z_axis = unit(forward)
    up_hint = np.asarray(up_hint, dtype=float)
    # Standard camera basis: right = forward x up, up = right x forward.
    x_axis = np.cross(z_axis, up_hint)
    if np.linalg.norm(x_axis) < 1e-9:
        raise CalibrationError(
            E_INVALID_PARAMETER,
            "projector forward and up vectors are parallel",
            {"forward": z_axis.tolist()},
        )
    x_axis = unit(x_axis)
    # Re-orthogonalize: up = forward x right (restores the handedness).
    y_axis = np.cross(z_axis, x_axis)
    return np.column_stack([x_axis, y_axis, z_axis])


def pixel_sensor_grid(width: int, height: int, fov_h_deg: float):
    """Normalised sensor coordinates for every pixel (z_camera == 1 plane)."""
    tan_h = np.tan(np.radians(fov_h_deg) / 2.0)
    tan_v = tan_h * height / width
    ii, jj = np.meshgrid(np.arange(width), np.arange(height), indexing="xy")
    sx = (2.0 * ii / (width - 1) - 1.0) * tan_h
    sy = (2.0 * jj / (height - 1) - 1.0) * tan_v
    return sx.astype(float), sy.astype(float), float(tan_h), float(tan_v)


def distort_radial(xu, yu, k1: float, k2: float):
    """Forward radial distortion: undistorted sensor coords -> distorted."""
    xu = np.asarray(xu, dtype=float)
    yu = np.asarray(yu, dtype=float)
    r2 = xu * xu + yu * yu
    factor = 1.0 + k1 * r2 + k2 * r2 * r2
    return xu * factor, yu * factor


def undistort_radial(xd, yd, k1: float, k2: float, tol: float = 1e-12,
                     max_iter: int = 60):
    """Inverse radial distortion via vectorised bisection on the radius.

    The validated distortion map ``f(ru) = ru(1+k1 ru^2+k2 ru^4)`` is
    monotone, so the scalar root is always bracketed.  Bisection cannot
    diverge the way a raw Newton step can for strongly barrel/pincushion
    coefficients at the sensor corner.
    """
    xd = np.asarray(xd, dtype=float)
    yd = np.asarray(yd, dtype=float)
    rd = np.sqrt(xd * xd + yd * yd)
    nonzero = rd > 1e-15
    lo = np.zeros_like(rd)
    # For a validated monotone lens the root always exists; grow the bracket
    # until it covers rd.  A hard ceiling guards against non-finite blow-up on
    # physically unreachable rays, which are then reported as NaN.
    hi = np.maximum(rd, 1e-9)
    hard_ceiling = 1e4
    hi2 = hi * hi
    with np.errstate(over="ignore", invalid="ignore"):
        f_hi = hi * (1.0 + k1 * hi2 + k2 * hi2 * hi2)
    already_bracketed = f_hi >= rd
    # Expand the upper bracket until f(hi) >= rd everywhere.
    for _ in range(60):
        hi2 = hi * hi
        with np.errstate(over="ignore", invalid="ignore"):
            f_hi = hi * (1.0 + k1 * hi2 + k2 * hi2 * hi2)
        grow = f_hi >= rd
        if np.all(grow | already_bracketed | (hi >= hard_ceiling)):
            break
        hi = np.where(grow | already_bracketed, hi,
                      np.minimum(2.0 * hi, hard_ceiling))
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        m2 = mid * mid
        f_mid = mid * (1.0 + k1 * m2 + k2 * m2 * m2)
        go_high = f_mid < rd
        lo = np.where(go_high, mid, lo)
        hi = np.where(go_high, hi, mid)
        if np.all(hi - lo <= tol * (1.0 + hi)):
            break
    ru = 0.5 * (lo + hi)
    # Rays whose distorted radius cannot be inverted inside the bounded sensor
    # envelope are physically unreachable; mark them NaN rather than returning
    # an exploded radius that corrupts downstream geometry.
    hi2 = hi * hi
    with np.errstate(over="ignore", invalid="ignore"):
        f_hi = hi * (1.0 + k1 * hi2 + k2 * hi2 * hi2)
    unreachable = f_hi < rd
    ru = np.where(unreachable, np.nan, ru)
    scale = np.where(nonzero, ru / np.where(nonzero, rd, 1.0), 1.0)
    return xd * scale, yd * scale


def validate_distortion(k1: float, k2: float, sensor_radius: float) -> None:
    """Reject out-of-range or non-monotonic radial distortion parameters."""
    if not (np.isfinite(k1) and np.isfinite(k2)):
        raise CalibrationError(
            E_DISTORTION_OUT_OF_RANGE,
            "distortion coefficients must be finite",
            {"k1": k1, "k2": k2},
        )
    if abs(k1) > K1_LIMIT or abs(k2) > K2_LIMIT:
        raise CalibrationError(
            E_DISTORTION_OUT_OF_RANGE,
            f"distortion coefficients outside +/-{K1_LIMIT} envelope",
            {"k1": k1, "k2": k2, "k1_limit": K1_LIMIT, "k2_limit": K2_LIMIT},
        )
    # Monotonic mapping keeps the inverse unique over the whole sensor.
    r2 = np.linspace(0.0, sensor_radius * sensor_radius, 256)
    derivative = 1.0 + 3.0 * k1 * r2 + 5.0 * k2 * r2 * r2
    if np.min(derivative) <= 0.0:
        raise CalibrationError(
            E_DISTORTION_OUT_OF_RANGE,
            "radial distortion is non-monotonic over the sensor diagonal",
            {"k1": k1, "k2": k2, "min_derivative": float(np.min(derivative))},
        )


def ray_sphere(origin, direction, center, radius: float):
    """Intersect rays with a sphere.

    Returns ``(points, hit)`` where ``points`` has shape (..., 3) and ``hit``
    is a boolean mask.  The nearest positive root is selected.
    """
    origin = np.asarray(origin, dtype=float)
    direction = np.asarray(direction, dtype=float)
    center = np.asarray(center, dtype=float)
    oc = origin - center
    half_b = np.einsum("...i,...i->...", direction, oc)
    c = np.einsum("...i,...i->...", oc, oc) - radius * radius
    disc = half_b * half_b - c
    hit = disc >= 0.0
    sqrt_disc = np.sqrt(np.where(hit, disc, 0.0))
    t_near = -half_b - sqrt_disc
    t_far = -half_b + sqrt_disc
    # Ray origin outside the sphere -> positive entry root.
    # Ray origin on/inside the sphere (t_near <= 0) -> exit root.
    t = np.where(t_near > 0.0, t_near, t_far)
    hit = hit & (t > 0.0)
    points = origin + direction * t[..., None]
    return points, hit


def world_to_dome_local(points, center, tilt: float) -> np.ndarray:
    """Rotate/translate world points into the dome local frame."""
    points = np.asarray(points, dtype=float)
    rel = points - np.asarray(center, dtype=float)
    return rel @ rot_x(tilt)


def dome_local_to_world(local, center, tilt: float) -> np.ndarray:
    local = np.asarray(local, dtype=float)
    return local @ rot_x(tilt).T + np.asarray(center, dtype=float)


def local_to_latlon(local, radius: float):
    """Upper-hemisphere local points -> (longitude [-pi, pi], latitude [0, pi/2])."""
    local = np.asarray(local, dtype=float)
    x, y, z = local[..., 0], local[..., 1], local[..., 2]
    lon = np.arctan2(y, x)
    lat = np.arcsin(np.clip(z / radius, -1.0, 1.0))
    return lon, lat


def latlon_to_local(lon, lat, radius: float) -> np.ndarray:
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    cl = np.cos(lat)
    return np.stack(
        [radius * cl * np.cos(lon), radius * cl * np.sin(lon), radius * np.sin(lat)],
        axis=-1,
    )
