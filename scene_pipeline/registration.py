"""Deterministic point-cloud registration / camera-pose refinement (ICP).

The initial camera extrinsics from the manifest are refined against the
canonical point cloud.  Every nearest-neighbour decision is fully determined
by distances and, on ties, by ascending point index.  The module also counts
exact nearest-neighbour comparisons so the scheduler can enforce the
registration comparison budget.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import numpy as np

from .hashing import checksum_numpy
from .manifest import CameraSpec


@dataclass
class CameraRegistration:
    camera_id: str
    transform: np.ndarray
    residual: float
    iterations: int
    comparisons: int
    candidates_per_iteration: int


@dataclass
class RegistrationResult:
    points: np.ndarray
    cameras: List[CameraRegistration]
    total_comparisons: int
    mean_residual: float
    max_residual: float
    comparison_budget: int
    downgraded: bool

    def to_payload(self) -> Dict[str, object]:
        return {
            "point_cloud_sha256": checksum_numpy(self.points),
            "total_comparisons": int(self.total_comparisons),
            "mean_residual": float(self.mean_residual),
            "max_residual": float(self.max_residual),
            "comparison_budget": int(self.comparison_budget),
            "downgraded": bool(self.downgraded),
            "cameras": [
                {
                    "camera_id": item.camera_id,
                    "transform": np.round(item.transform, 12).tolist(),
                    "residual": float(item.residual),
                    "iterations": int(item.iterations),
                    "comparisons": int(item.comparisons),
                    "candidates_per_iteration": int(item.candidates_per_iteration),
                }
                for item in self.cameras
            ],
        }


def _kabsch(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    source_centroid = source.mean(axis=0)
    target_centroid = target.mean(axis=0)
    source_zero = source - source_centroid
    target_zero = target - target_centroid
    covariance = source_zero.T @ target_zero
    u, _, vt = np.linalg.svd(covariance)
    rotation = vt.T @ u.T
    if np.linalg.det(rotation) < 0:
        vt[-1, :] *= -1.0
        rotation = vt.T @ u.T
    translation = target_centroid - rotation @ source_centroid
    transform = np.eye(4, dtype=np.float64)
    transform[:3, :3] = rotation
    transform[:3, 3] = translation
    return transform


def _project(points_camera: np.ndarray, camera: CameraSpec):
    z_values = points_camera[:, 2]
    valid = z_values > 1e-6
    u = np.round(camera.fx * points_camera[:, 0] / z_values + camera.cx).astype(np.int64)
    v = np.round(camera.fy * points_camera[:, 1] / z_values + camera.cy).astype(np.int64)
    inside = (
        valid & (u >= 0) & (u < camera.width) & (v >= 0) & (v < camera.height)
    )
    return u, v, inside


def _projection_correspondences(extrinsic: np.ndarray, camera: CameraSpec,
                                points: np.ndarray, depth: np.ndarray,
                                candidate_limit: int, comparisons: int):
    """Deterministic projection ICP correspondences.

    Each visible source point is compared against up to ``candidate_limit``
    depth observations in index order; the lowest-index matching pixel wins
    ties.  The comparison counter charges one comparison per candidate.
    """
    rotation = extrinsic[:3, :3]
    translation = extrinsic[:3, 3]
    camera_points = points @ rotation.T + translation[None, :]
    u, v, inside = _project(camera_points, camera)
    observed_v, observed_u = np.nonzero(depth > 0.0)
    observed_z = depth[observed_v, observed_u]
    observed_points = np.stack(
        [
            (observed_u - camera.cx) * observed_z / camera.fx,
            (observed_v - camera.cy) * observed_z / camera.fy,
            observed_z,
        ],
        axis=1,
    )
    limit = min(candidate_limit, observed_points.shape[0])
    source: list[np.ndarray] = []
    target: list[np.ndarray] = []
    for point_index in np.nonzero(inside)[0]:
        comparisons += limit
        if limit == 0:
            continue
        deltas = observed_points[:limit] - camera_points[point_index]
        squared = np.sum(deltas ** 2, axis=1)
        best = int(np.argmin(squared))  # first index wins distance ties
        source.append(camera_points[point_index])
        target.append(observed_points[best])
    if not source:
        return (np.empty((0, 3)), np.empty((0, 3)), comparisons)
    return (
        np.ascontiguousarray(np.array(source, dtype=np.float64)),
        np.ascontiguousarray(np.array(target, dtype=np.float64)),
        comparisons,
    )


def _refine_camera(camera: CameraSpec, points: np.ndarray,
                   depth: np.ndarray, max_iterations: int, tolerance: float,
                   candidate_limit: int) -> CameraRegistration:
    extrinsic = np.asarray(camera.extrinsic, dtype=np.float64)
    comparisons = 0
    iterations = 0
    for iteration in range(1, max_iterations + 1):
        iterations = iteration
        source, target, comparisons = _projection_correspondences(
            extrinsic, camera, points, depth, candidate_limit, comparisons
        )
        if source.shape[0] < 4:
            break
        # Rigid correction in camera frame, then propagated to the extrinsic.
        correction = _kabsch(source, target)
        extrinsic = correction @ extrinsic
        shift = np.linalg.norm(correction[:3, 3])
        if shift < tolerance and iteration >= 2:
            break
    # Final correspondence pass used for the reported RMS residual.
    source, target, comparisons = _projection_correspondences(
        extrinsic, camera, points, depth, candidate_limit, comparisons
    )
    if source.shape[0] == 0:
        residual = 0.0
    else:
        residual = float(np.sqrt(np.mean(np.sum((source - target) ** 2, axis=1))))
    return CameraRegistration(
        camera_id=camera.camera_id,
        transform=np.ascontiguousarray(extrinsic),
        residual=residual,
        iterations=iterations,
        comparisons=comparisons,
        candidates_per_iteration=candidate_limit,
    )


def plan_registration_budget(camera_count: int, point_count: int,
                             max_iterations: int,
                             comparison_budget: int) -> tuple[int, int, bool]:
    """Derive a deterministic per-iteration candidate cap.

    Returns ``(candidate_limit, iterations, downgraded)``.  First iterations
    are trimmed, then candidate counts, so the exact comparison count never
    exceeds ``comparison_budget``.
    """
    iterations = max_iterations
    downgraded = False
    # One extra nearest pass per camera is used for the final residual.
    passes = iterations + 1
    per_camera = comparison_budget // max(1, camera_count)
    candidate_limit = point_count
    if camera_count * point_count * passes > comparison_budget:
        downgraded = True
        candidate_limit = max(1, per_camera // passes)
        while camera_count * candidate_limit * passes > comparison_budget and iterations > 1:
            iterations -= 1
            passes = iterations + 1
            candidate_limit = max(1, per_camera // passes)
    return min(candidate_limit, point_count), iterations, downgraded


def register_scene(bundle, icp_config, comparison_budget: int) -> RegistrationResult:
    """Run deterministic ICP for every camera of one observation bundle."""
    points = np.ascontiguousarray(bundle.points, dtype=np.float64)
    camera_count = len(bundle.cameras)
    candidate_limit, iterations, downgraded = plan_registration_budget(
        camera_count=camera_count,
        point_count=points.shape[0],
        max_iterations=icp_config.max_iterations,
        comparison_budget=comparison_budget,
    )
    cameras: List[CameraRegistration] = []
    total_comparisons = 0
    for camera in bundle.cameras:
        registration = _refine_camera(
            camera, points, bundle.depth_maps[camera.camera_id],
            max_iterations=max(icp_config.min_iterations, iterations),
            tolerance=icp_config.tolerance,
            candidate_limit=candidate_limit,
        )
        cameras.append(registration)
        total_comparisons += registration.comparisons

    residuals = np.array([item.residual for item in cameras], dtype=np.float64)
    return RegistrationResult(
        points=points,
        cameras=cameras,
        total_comparisons=int(total_comparisons),
        mean_residual=float(np.mean(residuals)),
        max_residual=float(np.max(residuals)),
        comparison_budget=int(comparison_budget),
        downgraded=downgraded,
    )
