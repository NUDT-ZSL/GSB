"""Deterministic texture baking and multi-view consistency checks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import numpy as np

from .errors import TextureConsistencyError
from .hashing import checksum_numpy
from .geometry import Mesh


@dataclass
class ViewConsistency:
    camera_id: str
    observed_pixels: int
    mean_deviation: float
    max_deviation: float
    within_tolerance: bool


@dataclass
class TextureResult:
    texture: np.ndarray
    uv: np.ndarray
    view_reports: List[ViewConsistency]
    tolerance: float
    resolution: int
    downgraded: bool

    def to_payload(self) -> Dict[str, object]:
        return {
            "texture_sha256": checksum_numpy(self.texture),
            "uv_sha256": checksum_numpy(self.uv),
            "tolerance": float(self.tolerance),
            "resolution": int(self.resolution),
            "downgraded": bool(self.downgraded),
            "view_reports": [
                {
                    "camera_id": report.camera_id,
                    "observed_pixels": int(report.observed_pixels),
                    "mean_deviation": float(report.mean_deviation),
                    "max_deviation": float(report.max_deviation),
                    "within_tolerance": bool(report.within_tolerance),
                }
                for report in self.view_reports
            ],
        }


def estimate_texture_units(resolution: int) -> int:
    # Three uint8 colour channels per texel.
    return int(resolution * resolution * 3)


def planar_uv(vertices: np.ndarray) -> np.ndarray:
    lower = vertices.min(axis=0)
    extent = np.ptp(vertices, axis=0)
    extent[extent == 0.0] = 1.0
    uv = (vertices - lower) / extent
    return np.ascontiguousarray(uv[:, :2], dtype=np.float64)


def _nearest_point_colors(vertices: np.ndarray, points: np.ndarray,
                          colors: np.ndarray) -> np.ndarray:
    deltas = vertices[:, None, :] - points[None, :, :]
    distances = np.einsum("vpk,vpk->vp", deltas, deltas)
    nearest = np.argmin(distances, axis=1)
    return colors[nearest].astype(np.float64)


def bake_texture(mesh: Mesh, points: np.ndarray, colors: np.ndarray,
                 resolution: int) -> tuple[np.ndarray, np.ndarray]:
    """Bake nearest-point vertex colours into a planar RGB texture."""
    uv = planar_uv(mesh.vertices)
    vertex_colors = _nearest_point_colors(mesh.vertices, points, colors)
    texture = np.zeros((resolution, resolution, 3), dtype=np.uint8)
    # Fill texels from nearest vertices; ties resolve by lowest vertex index.
    texel_coords = np.stack(
        np.meshgrid(np.arange(resolution), np.arange(resolution), indexing="xy"),
        axis=-1,
    ).reshape(-1, 2).astype(np.float64)
    texel_uv = (texel_coords + 0.5) / resolution
    distances = np.linalg.norm(
        texel_uv[:, None, :] - uv[None, :, :], axis=2
    )
    nearest_vertex = np.argmin(distances, axis=1)
    for flat_index, vertex_index in enumerate(nearest_vertex):
        u, v = texel_coords[flat_index].astype(np.int64)
        texture[v, u] = np.round(vertex_colors[vertex_index]).astype(np.uint8)
    return np.ascontiguousarray(texture), uv


def _sample_texture(texture: np.ndarray, uv_values: np.ndarray) -> np.ndarray:
    resolution = texture.shape[0]
    u = np.clip(
        np.round(uv_values[:, 0] * resolution - 0.5).astype(np.int64),
        0, resolution - 1,
    )
    v = np.clip(
        np.round(uv_values[:, 1] * resolution - 0.5).astype(np.int64),
        0, resolution - 1,
    )
    return texture[v, u].astype(np.float64)


def check_consistency(mesh: Mesh, uv: np.ndarray, texture: np.ndarray,
                      bundle, cameras_transforms: Dict[str, np.ndarray]
                      ) -> List[ViewConsistency]:
    """Reproject baked vertex colours into each camera and compare observations."""
    reports: List[ViewConsistency] = []
    vertex_colors = _sample_texture(texture, uv)
    for camera in bundle.cameras:
        extrinsic = cameras_transforms[camera.camera_id]
        rotation = extrinsic[:3, :3]
        translation = extrinsic[:3, 3]
        camera_vertices = mesh.vertices @ rotation.T + translation[None, :]
        depth = bundle.depth_maps[camera.camera_id]
        observed_image = bundle.color_images[camera.camera_id]
        deviations: List[float] = []
        for index, vertex in enumerate(camera_vertices):
            x_cam, y_cam, z_cam = vertex
            if z_cam <= 1e-6:
                continue
            u = int(round(camera.fx * x_cam / z_cam + camera.cx))
            v = int(round(camera.fy * y_cam / z_cam + camera.cy))
            if not (0 <= u < camera.width and 0 <= v < camera.height):
                continue
            observed_depth = depth[v, u]
            # A vertex corresponds to an observation when it projects inside
            # the observed silhouette with compatible range.  Requiring exact
            # pixel equality would make sparse point renders untestable.
            if observed_depth <= 0.0 or abs(observed_depth - z_cam) > 0.25:
                continue
            expected = observed_image[v, u].astype(np.float64)
            deviation = float(
                np.linalg.norm(vertex_colors[index] - expected) / np.sqrt(3.0) / 255.0
            )
            deviations.append(deviation)
        if not deviations:
            reports.append(ViewConsistency(
                camera_id=camera.camera_id, observed_pixels=0,
                mean_deviation=0.0, max_deviation=0.0,
                within_tolerance=True,
            ))
            continue
        deviation_array = np.asarray(deviations, dtype=np.float64)
        reports.append(ViewConsistency(
            camera_id=camera.camera_id,
            observed_pixels=int(deviation_array.size),
            mean_deviation=float(deviation_array.mean()),
            max_deviation=float(deviation_array.max()),
            within_tolerance=bool(deviation_array.max() <= 1.0),
        ))
    return reports


def process_texture(geometry_result, registration_result, bundle,
                    resolution: int, tolerance: float,
                    downgraded: bool = False) -> TextureResult:
    texture, uv = bake_texture(
        geometry_result.mesh, registration_result.points,
        bundle.colors, resolution,
    )
    transforms = {
        item.camera_id: item.transform for item in registration_result.cameras
    }
    reports = check_consistency(
        geometry_result.mesh, uv, texture, bundle, transforms
    )
    for report in reports:
        if not report.within_tolerance or report.max_deviation > tolerance:
            raise TextureConsistencyError(
                f"texture consistency exceeded for camera {report.camera_id}",
                scene_id=bundle.manifest.scene_id,
                stage="texture",
                details={
                    "camera_id": report.camera_id,
                    "max_deviation": report.max_deviation,
                    "tolerance": float(tolerance),
                    "excess": max(0.0, report.max_deviation - tolerance),
                },
            )
    return TextureResult(
        texture=texture,
        uv=uv,
        view_reports=reports,
        tolerance=float(tolerance),
        resolution=int(resolution),
        downgraded=downgraded,
    )
