"""Area-averaged thumbnails with conservation and fracture diagnostics."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np

from .models import Camera, PackagingProject
from .rasterizer import RasterResult, rasterize_strip

DEFAULT_TOLERANCE = 0.02


@dataclass
class ThumbnailResult:
    thumb_width: int
    thumb_height: int
    factor_x: int
    factor_y: int
    averaged: np.ndarray
    direct: np.ndarray
    difference: np.ndarray
    attenuation: np.ndarray
    fracture: np.ndarray
    tolerance: float
    full_total: float
    thumb_total: float
    conservation_error: float
    direct_total: float
    marked_pixels: List[Tuple[int, int]]


def declared_thumbnail_size(width: int, height: int,
                            factor: int = 4) -> Tuple[int, int]:
    return width // factor, height // factor


def area_average_downsample(coverage: np.ndarray, thumb_width: int,
                            thumb_height: int) -> Tuple[np.ndarray, int, int]:
    height, width = coverage.shape
    if width % thumb_width or height % thumb_height:
        raise ValueError("thumbnail size must divide the full resolution evenly")
    factor_x = width // thumb_width
    factor_y = height // thumb_height
    trimmed = coverage[:thumb_height * factor_y, :thumb_width * factor_x]
    blocks = trimmed.reshape(thumb_height, factor_y, thumb_width, factor_x)
    averaged = blocks.mean(axis=(1, 3))
    return averaged, factor_x, factor_y


def _rescaled_curves(project: PackagingProject, camera: Camera):
    """Rebind curve radii from full-resolution pixels to thumbnail pixels."""
    scale = camera.width_px / project.camera.width_px
    curves = []
    for curve in project.valid_curves():
        bound = dict(curve.__dict__)
        bound["radius_px"] = curve.radius_px * scale
        curves.append(type(curve)(**bound))
    return curves


def build_thumbnail(project: PackagingProject, full: RasterResult,
                    thumb_width: int, thumb_height: int,
                    tolerance: float = DEFAULT_TOLERANCE) -> ThumbnailResult:
    averaged, factor_x, factor_y = area_average_downsample(
        full.coverage, thumb_width, thumb_height)

    direct_camera = full_camera_scaled(project, thumb_width, thumb_height)
    direct_project = PackagingProject(
        project_id=project.project_id, seed=project.seed, version=project.version,
        outline_bounds=project.outline_bounds, body=project.body, cap=project.cap,
        die_lines=project.die_lines, labels=project.labels, lights=project.lights,
        camera=direct_camera, curves=_rescaled_curves(project, direct_camera),
        material_of_surface=project.material_of_surface,
        issues=list(project.issues),
    )
    direct_result = rasterize_strip(direct_project, direct_camera)
    direct = direct_result.coverage

    difference = averaged - direct
    attenuation = difference > tolerance
    reference_level = np.maximum(averaged, 1e-12)
    fracture = (difference > tolerance) & (direct < reference_level * 0.2)
    marked = [(int(x), int(y)) for y, x in zip(*np.nonzero(fracture | attenuation))]

    full_total = float(full.coverage.sum())
    thumb_total = float(averaged.sum() * factor_x * factor_y)
    return ThumbnailResult(
        thumb_width=thumb_width,
        thumb_height=thumb_height,
        factor_x=factor_x,
        factor_y=factor_y,
        averaged=averaged,
        direct=direct,
        difference=difference,
        attenuation=attenuation,
        fracture=fracture,
        tolerance=tolerance,
        full_total=full_total,
        thumb_total=thumb_total,
        conservation_error=abs(full_total - thumb_total),
        direct_total=float(direct.sum()),
        marked_pixels=marked,
    )


def full_camera_scaled(project: PackagingProject, width: int, height: int) -> Camera:
    return project.camera.scaled(width, height)


def thumbnail_payload(result: ThumbnailResult) -> Dict:
    return {
        "thumb_width": result.thumb_width,
        "thumb_height": result.thumb_height,
        "factor": [result.factor_x, result.factor_y],
        "tolerance": result.tolerance,
        "full_total": result.full_total,
        "thumb_total_scaled": result.thumb_total,
        "direct_total": result.direct_total,
        "conservation_error": result.conservation_error,
        "attenuation_pixels": int(result.attenuation.sum()),
        "fracture_pixels": int(result.fracture.sum()),
        "marked_pixels": result.marked_pixels[:200],
    }
