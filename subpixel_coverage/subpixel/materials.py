"""Material region masks and coverage statistics."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import numpy as np

from .geometry import point_in_polygon
from .models import Camera, PackagingProject

MATERIALS = ("label", "cap_matte", "frosted_glass")


def _silhouette_polygon(project: PackagingProject, camera: Camera,
                        surface_name: str) -> np.ndarray:
    surface = project.surface_by_name(surface_name)
    theta = np.linspace(surface.theta_min, surface.theta_max, 96)
    bottom = np.column_stack([
        surface.radius * np.sin(theta),
        np.full_like(theta, surface.y_min),
        surface.radius * np.cos(theta),
    ])
    top = np.column_stack([
        surface.radius * np.sin(theta),
        np.full_like(theta, surface.y_max),
        surface.radius * np.cos(theta),
    ])
    polygon_3d = np.vstack([bottom, top[::-1]])
    xy, _depth = camera.project(polygon_3d)
    return xy


def material_masks(project: PackagingProject, camera: Camera) -> Dict[str, np.ndarray]:
    """Rasterise projected surface silhouettes into per-material masks."""
    width, height = camera.width_px, camera.height_px
    yy, xx = np.meshgrid(np.arange(height), np.arange(width), indexing="ij")
    centers = np.column_stack([xx.ravel() + 0.5, yy.ravel() + 0.5])

    body_poly = _silhouette_polygon(project, camera, "body")
    cap_poly = _silhouette_polygon(project, camera, "cap")
    body_mask = point_in_polygon(centers, body_poly).reshape(height, width)
    cap_mask = point_in_polygon(centers, cap_poly).reshape(height, width)

    label = project.labels[0]
    theta = np.linspace(label.theta_range[0], label.theta_range[1], 64)
    lower = np.column_stack([
        project.body.radius * np.sin(theta),
        np.full_like(theta, label.y_range[0]),
        project.body.radius * np.cos(theta),
    ])
    upper = np.column_stack([
        project.body.radius * np.sin(theta),
        np.full_like(theta, label.y_range[1]),
        project.body.radius * np.cos(theta),
    ])
    label_poly, _ = camera.project(np.vstack([lower, upper[::-1]]))
    label_mask = point_in_polygon(centers, label_poly).reshape(height, width)

    glass_mask = body_mask & ~label_mask
    return {
        "label": label_mask,
        "cap_matte": cap_mask,
        "frosted_glass": glass_mask,
    }


@dataclass
class MaterialStats:
    material: str
    pixel_count: int
    covered_pixels: int
    total_coverage: float
    mean_coverage: float
    fiber_contribution: float
    grain_contribution: float


def material_statistics(project: PackagingProject, coverage: np.ndarray,
                        contributions: np.ndarray,
                        masks: Dict[str, np.ndarray]) -> List[MaterialStats]:
    kind_by_curve = {curve.curve_id: curve.kind for curve in project.curves}
    stats: List[MaterialStats] = []
    for material in MATERIALS:
        mask = masks[material]
        region = coverage * mask
        pixel_count = int(mask.sum())
        total = float(region.sum())
        covered = int((region > 0.0).sum())
        fiber_total = 0.0
        grain_total = 0.0
        if len(contributions):
            in_mask = mask[contributions["y"], contributions["x"]]
            rows = contributions[in_mask]
            for row in rows:
                kind = kind_by_curve.get(str(row["curve_id"]), "fiber")
                if kind == "grain":
                    grain_total += float(row["alpha"])
                else:
                    fiber_total += float(row["alpha"])
        stats.append(MaterialStats(
            material=material,
            pixel_count=pixel_count,
            covered_pixels=covered,
            total_coverage=total,
            mean_coverage=total / pixel_count if pixel_count else 0.0,
            fiber_contribution=fiber_total,
            grain_contribution=grain_total,
        ))
    return stats


def stats_to_dicts(stats: List[MaterialStats]) -> List[Dict]:
    return [
        {
            "material": item.material,
            "pixel_count": item.pixel_count,
            "covered_pixels": item.covered_pixels,
            "total_coverage": item.total_coverage,
            "mean_coverage": item.mean_coverage,
            "fiber_contribution": item.fiber_contribution,
            "grain_contribution": item.grain_contribution,
        }
        for item in stats
    ]
