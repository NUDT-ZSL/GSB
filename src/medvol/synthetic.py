"""Deterministic local CT-like phantom generation (no external DICOM data)."""

from __future__ import annotations

import numpy as np

from .volume import Volume


def make_synthetic_ct_volume(size: int = 128) -> Volume:
    """Create a deterministic sphere/cylinder phantom stored as uint16.

    Values intentionally span a CT-like display range after a +1024 HU offset:
    air-like background is 0, soft tissue is roughly 350-1000, and high-density
    structures are roughly 2800-4000. The final array is always uint16 and
    contains smooth deterministic texture, with no RNG involved.
    """

    if size < 32:
        raise ValueError("size must be at least 32")
    index = np.arange(size, dtype=np.float64)
    z, y, x = np.meshgrid(index, index, index, indexing="ij")
    center = (size - 1) * 0.5

    data = np.zeros((size, size, size), dtype=np.float64)

    # Ellipsoidal soft-tissue body.
    body_radius = (
        ((x - center) / (0.43 * size)) ** 2
        + ((y - center) / (0.39 * size)) ** 2
        + ((z - center) / (0.45 * size)) ** 2
    )
    body = body_radius <= 1.0
    body_gradient = 650.0 + 260.0 * (1.0 - np.clip(body_radius, 0.0, 1.0))
    texture = 35.0 * np.sin(x * 0.18) * np.sin(y * 0.14) * np.cos(z * 0.17)
    data[body] = body_gradient[body] + texture[body]

    # A low-density internal cavity gives visible transfer-function boundaries.
    cavity = (
        ((x - center + 0.09 * size) / (0.20 * size)) ** 2
        + ((y - center - 0.04 * size) / (0.15 * size)) ** 2
        + ((z - center - 0.03 * size) / (0.22 * size)) ** 2
    ) <= 1.0
    data[cavity] = 190.0 + 45.0 * np.cos((x[cavity] + z[cavity]) * 0.16)

    # High-density spherical inclusion with a radial bone-like ramp.
    sphere_radius = 0.20 * size
    distance = np.sqrt(
        (x - (center - 0.10 * size)) ** 2
        + (y - (center + 0.10 * size)) ** 2
        + (z - (center - 0.06 * size)) ** 2
    )
    sphere = distance <= sphere_radius
    data[sphere] = 2750.0 + 1100.0 * (1.0 - distance[sphere] / sphere_radius) ** 2

    # Two perpendicular high-density cylinders.
    cylinder_z = ((x - center - 0.23 * size) ** 2 + (y - center - 0.03 * size) ** 2) <= (0.075 * size) ** 2
    cylinder_x = ((y - center - 0.17 * size) ** 2 + (z - center + 0.10 * size) ** 2) <= (0.060 * size) ** 2
    data[cylinder_z] = 3350.0 + 150.0 * np.sin(z[cylinder_z] * 0.10)
    data[cylinder_x] = 3150.0 + 120.0 * np.cos(x[cylinder_x] * 0.12)

    # A small deterministic high-density granule.
    granule = (
        ((x - center + 0.15 * size) / (0.035 * size)) ** 2
        + ((y - center + 0.10 * size) / (0.030 * size)) ** 2
        + ((z - center + 0.15 * size) / (0.040 * size)) ** 2
    ) <= 1.0
    data[granule] = 3600.0

    data = np.clip(np.floor(data + 0.5), 0, 4095).astype(np.uint16)
    return Volume(data, spacing=(1.0, 1.0, 1.0), origin=(0.0, 0.0, 0.0))

