"""Fixed-seed sample project generator.

The generator is fully deterministic: the same seed always produces the same
dome geometry, projector layout, lens coefficients and calibration control
points.  No external devices, services or data sets are required.
"""

from __future__ import annotations

import numpy as np

from .errors import CalibrationError, E_EMPTY_LAYOUT
from .geometry import (
    make_basis,
    ray_sphere,
    world_to_dome_local,
    local_to_latlon,
    distort_radial,
    validate_distortion,
)
from .models import (
    DomeSpec,
    ProjectorSpec,
    ControlPointSet,
    BlendingParams,
    CalibrationProject,
)


def _default_layout(rng: np.random.Generator, radius: float):
    """Four projectors on a ring, each aimed at the hemisphere centre."""
    ring_radius = radius * 0.72
    ring_z = radius * 0.95
    specs = []
    for idx in range(4):
        az = 2.0 * np.pi * idx / 4.0
        position = (ring_radius * np.cos(az), ring_radius * np.sin(az), ring_z)
        forward = tuple(-np.asarray(position, dtype=float))
        up_hint = (0.0, 0.0, 1.0)
        # Mild deterministic variation so projectors are not exact copies.
        fov = 120.0 + float(rng.uniform(-2.0, 2.0))
        # Reject coefficient draws that break radial monotonicity so every
        # seed yields a physically valid lens (validated again below).
        sensor_radius = np.sqrt(
            np.tan(np.radians(122.0) / 2.0) ** 2
            + (np.tan(np.radians(122.0) / 2.0) * 240 / 320) ** 2
        )
        while True:
            k1 = float(rng.uniform(-0.03, 0.01))
            k2 = float(rng.uniform(-0.004, 0.004))
            r2 = np.linspace(0.0, sensor_radius ** 2, 256)
            if np.min(1.0 + 3.0 * k1 * r2 + 5.0 * k2 * r2 * r2) > 0.0:
                break
        specs.append(
            ProjectorSpec(
                projector_id=f"P{idx + 1}",
                position=tuple(float(v) for v in position),
                forward=tuple(float(v) for v in forward),
                up_hint=up_hint,
                fov_h_deg=float(fov),
                width=320,
                height=240,
                k1=k1,
                k2=k2,
            )
        )
    return specs


def _warp_field(lon, lat, coeffs: dict):
    """Known smooth residual field used to synthesise 'measured' points.

    The same field is evaluated by the TPS tests, which verify that the fitted
    correction recovers the ideal coordinates within a reported tolerance.
    """
    dlon = (
        coeffs["a1"] * np.sin(coeffs["f1"] * lon + coeffs["p1"])
        + coeffs["a2"] * np.sin(coeffs["b1"] * lat)
    )
    dlat = (
        coeffs["c1"] * np.sin(coeffs["g1"] * lat + coeffs["q1"])
        + coeffs["c2"] * np.sin(coeffs["h1"] * lon)
    )
    return dlon, dlat


def _grid_latlon(grid_cols: int, grid_rows: int, fov_h_deg: float,
                 aspect: float, calib_frac: float, spec: ProjectorSpec,
                 dome: DomeSpec, k1: float, k2: float):
    """Project a sparse calibration grid onto the dome -> ideal lon/lat."""
    tan_h = np.tan(np.radians(fov_h_deg) / 2.0)
    tan_v = tan_h * aspect
    # Restrict the calibration grid to a central region so every point lands
    # comfortably on the upper hemisphere.
    frac = float(calib_frac)
    cu = np.linspace(-tan_h * (1.0 - 2.0 * frac), tan_h * (1.0 - 2.0 * frac), grid_cols)
    cv = np.linspace(-tan_v * (1.0 - 2.0 * frac), tan_v * (1.0 - 2.0 * frac), grid_rows)
    gu, gv = np.meshgrid(cu, cv, indexing="xy")
    xu, yu = gu.ravel(), gv.ravel()
    xd, yd = distort_radial(xu, yu, k1, k2)
    basis = make_basis(np.asarray(spec.forward), np.asarray(spec.up_hint))
    dirs = np.column_stack([xd, yd, np.ones_like(xd)]) @ basis.T
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
    points, hit = ray_sphere(np.asarray(spec.position), dirs,
                             np.asarray(dome.center), dome.radius)
    local = world_to_dome_local(points, np.asarray(dome.center), dome.tilt)
    upper = local[..., 2] >= -1e-9
    if not np.all(hit & upper):
        raise CalibrationError(
            "E1003_PROJECTOR_DOME_NO_OVERLAP",
            "calibration grid does not fully land on the upper hemisphere",
            {"projector_id": spec.projector_id},
        )
    lon, lat = local_to_latlon(local, dome.radius)
    return lon, lat


def generate_sample_project(seed: int = 20261128,
                            projector_specs: list[ProjectorSpec] | None = None,
                            grid_width: int = 1440,
                            grid_height: int = 360) -> CalibrationProject:
    """Generate a deterministic, self-contained sample calibration project."""
    rng = np.random.default_rng(seed)
    dome = DomeSpec(radius=10.0, center=(0.0, 0.0, 0.0), tilt_deg=0.0)

    if projector_specs is None:
        projector_specs = _default_layout(rng, dome.radius)
    if not projector_specs:
        raise CalibrationError(E_EMPTY_LAYOUT, "no projectors in sample layout")

    for spec in projector_specs:
        sensor_radius = np.sqrt(
            np.tan(np.radians(spec.fov_h_deg) / 2.0) ** 2
            + (np.tan(np.radians(spec.fov_h_deg) / 2.0) * spec.height / spec.width) ** 2
        )
        validate_distortion(spec.k1, spec.k2, sensor_radius)

    control_points: list[ControlPointSet] = []
    warp_params: dict = {}
    for spec in projector_specs:
        lon_ideal, lat_ideal = _grid_latlon(
            grid_cols=10, grid_rows=8, fov_h_deg=spec.fov_h_deg,
            aspect=spec.height / spec.width, calib_frac=spec.calib_frac,
            spec=spec, dome=dome,
            k1=spec.k1, k2=spec.k2,
        )
        n = lon_ideal.size
        coeffs = {
            "a1": float(rng.uniform(0.00020, 0.00060)),
            "a2": float(rng.uniform(0.00015, 0.00045)),
            "f1": float(rng.uniform(1.5, 2.5)),
            "p1": float(rng.uniform(0.0, np.pi)),
            "b1": float(rng.uniform(1.5, 3.0)),
            "c1": float(rng.uniform(0.00020, 0.00060)),
            "c2": float(rng.uniform(0.00015, 0.00045)),
            "g1": float(rng.uniform(1.5, 2.5)),
            "q1": float(rng.uniform(0.0, np.pi)),
            "h1": float(rng.uniform(1.5, 3.0)),
        }
        warp_lon, warp_lat = _warp_field(lon_ideal, lat_ideal, coeffs)
        # Perturbation magnitude grows with the point index (requirement 1).
        index = np.arange(n)
        noise_scale = float(rng.uniform(2e-4, 6e-4))
        amplitude = noise_scale * (1.0 + index / max(n - 1, 1))
        noise_lon = rng.normal(0.0, 1.0, size=n) * amplitude
        noise_lat = rng.normal(0.0, 1.0, size=n) * amplitude
        lon_obs = lon_ideal + warp_lon + noise_lon
        lat_obs = lat_ideal + warp_lat + noise_lat
        control_points.append(
            ControlPointSet(
                projector_id=spec.projector_id,
                ideal_lon=lon_ideal,
                ideal_lat=lat_ideal,
                observed_lon=lon_obs,
                observed_lat=lat_obs,
                noise_scale=noise_scale,
            )
        )
        warp_params[spec.projector_id] = coeffs

    blending = BlendingParams(feather_width_deg=6.0, curve="smoothstep", gamma=1.0)
    project = CalibrationProject(
        seed=int(seed),
        dome=dome,
        projectors=projector_specs,
        control_points=control_points,
        blending=blending,
        grid_width=int(grid_width),
        grid_height=int(grid_height),
        warp_params=warp_params,
        projector_versions={spec.projector_id: 1 for spec in projector_specs},
    )
    return project


def evaluate_warp(project: CalibrationProject, projector_id: str, lon, lat):
    """Evaluate the generator's known residual field (used by acceptance tests)."""
    coeffs = project.warp_params[projector_id]
    return _warp_field(lon, lat, coeffs)
