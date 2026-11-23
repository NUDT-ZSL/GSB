"""Deterministic point-to-point ICP registration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class RegistrationResult:
    world_from_camera: np.ndarray
    residual: float
    iterations: int
    distances: np.ndarray
    nearest_indices: np.ndarray

    def to_dict(self) -> dict[str, Any]:
        return {
            "world_from_camera": self.world_from_camera,
            "residual": float(self.residual),
            "iterations": int(self.iterations),
            "distances": self.distances,
            "nearest_indices": self.nearest_indices,
        }


def _validate_transform(transform: np.ndarray) -> np.ndarray:
    matrix = np.asarray(transform, dtype=np.float64)
    if matrix.shape != (4, 4):
        raise ValueError("world_from_camera must have shape (4, 4)")
    if not np.all(np.isfinite(matrix)):
        raise ValueError("world_from_camera contains a non-finite number")
    return matrix.copy()


def transform_points(points: np.ndarray, transform: np.ndarray) -> np.ndarray:
    source = np.asarray(points, dtype=np.float64)
    matrix = _validate_transform(transform)
    return source @ matrix[:3, :3].T + matrix[:3, 3]


def _nearest_target_indices(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    # Direct subtraction avoids cancellation in the expanded distance formula.
    indices = np.empty(source.shape[0], dtype=np.int64)
    for start in range(0, source.shape[0], 256):
        stop = min(start + 256, source.shape[0])
        difference = source[start:stop, None, :] - target[None, :, :]
        squared = np.sum(difference * difference, axis=2)
        indices[start:stop] = np.argmin(squared, axis=1)
    return indices


def registration_residual(
    world_points: np.ndarray,
    source_points_camera: np.ndarray,
    world_from_camera: np.ndarray,
) -> tuple[float, np.ndarray, np.ndarray]:
    target = np.asarray(world_points, dtype=np.float64)
    source = np.asarray(source_points_camera, dtype=np.float64)
    if target.ndim != 2 or target.shape[1] != 3:
        raise ValueError("world_points must have shape (N, 3)")
    if source.ndim != 2 or source.shape[1] != 3:
        raise ValueError("source_points_camera must have shape (M, 3)")
    if source.shape[0] == 0:
        raise ValueError("source_points_camera cannot be empty")
    transformed = transform_points(source, world_from_camera)
    nearest = _nearest_target_indices(transformed, target)
    matched = target[nearest]
    squared = np.sum((transformed - matched) ** 2, axis=1)
    distances = np.sqrt(np.maximum(squared, 0.0))
    residual = float(np.sqrt(np.mean(squared)))
    return residual, distances, nearest


def _kabsch_update(
    transformed: np.ndarray,
    matched: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    source_mean = np.mean(transformed, axis=0)
    target_mean = np.mean(matched, axis=0)
    source_centered = transformed - source_mean
    target_centered = matched - target_mean
    covariance = source_centered.T @ target_centered
    u_matrix, _singular_values, vt_matrix = np.linalg.svd(covariance)
    rotation = vt_matrix.T @ u_matrix.T
    if float(np.linalg.det(rotation)) < 0.0:
        vt_matrix = vt_matrix.copy()
        vt_matrix[-1, :] *= -1.0
        rotation = vt_matrix.T @ u_matrix.T
    translation = target_mean - rotation @ source_mean
    return rotation, translation


def register_point_cloud(
    world_points: np.ndarray,
    source_points_camera: np.ndarray,
    initial_world_from_camera: np.ndarray,
    *,
    max_iterations: int = 30,
    tolerance: float = 1e-10,
) -> RegistrationResult:
    target = np.asarray(world_points, dtype=np.float64)
    source = np.asarray(source_points_camera, dtype=np.float64)
    transform = _validate_transform(initial_world_from_camera)
    if target.ndim != 2 or target.shape[1] != 3:
        raise ValueError("world_points must have shape (N, 3)")
    if source.ndim != 2 or source.shape[1] != 3:
        raise ValueError("source_points_camera must have shape (M, 3)")
    if int(max_iterations) < 1:
        raise ValueError("max_iterations must be >= 1")
    if float(tolerance) < 0.0:
        raise ValueError("tolerance must be >= 0")

    iterations = 0
    for iteration in range(int(max_iterations)):
        iterations = iteration + 1
        transformed = transform_points(source, transform)
        nearest = _nearest_target_indices(transformed, target)
        matched = target[nearest]
        rotation_delta, translation_delta = _kabsch_update(transformed, matched)
        rotation = rotation_delta @ transform[:3, :3]
        translation = rotation_delta @ transform[:3, 3] + translation_delta
        transform = np.eye(4, dtype=np.float64)
        transform[:3, :3] = rotation
        transform[:3, 3] = translation
        rotation_error = float(
            np.linalg.norm(rotation_delta - np.eye(3, dtype=np.float64))
        )
        if rotation_error + float(np.linalg.norm(translation_delta)) <= float(
            tolerance
        ):
            break

    residual, distances, nearest = registration_residual(
        target, source, transform
    )
    return RegistrationResult(
        world_from_camera=transform,
        residual=residual,
        iterations=int(iterations),
        distances=np.ascontiguousarray(distances, dtype=np.float64),
        nearest_indices=np.ascontiguousarray(nearest, dtype=np.int64),
    )

