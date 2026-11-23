"""Deterministic weighted fusion and edge-collapse simplification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .errors import FaceBudgetExceeded
from .geometry import WorldGeometry


@dataclass(frozen=True)
class FusedMesh:
    vertices: np.ndarray
    faces: np.ndarray
    uv: np.ndarray
    raw_weight_sums: np.ndarray
    normalized_weight_sums: np.ndarray
    normalization_errors: np.ndarray
    contributed_vertices: int

    @property
    def normalization_interval(self) -> tuple[float, float]:
        if self.normalization_errors.size == 0:
            return (0.0, 0.0)
        return (
            float(np.min(self.normalization_errors)),
            float(np.max(self.normalization_errors)),
        )


@dataclass(frozen=True)
class MeshSimplificationResult:
    vertices: np.ndarray
    faces: np.ndarray
    uv: np.ndarray
    history: tuple[dict[str, Any], ...]
    faces_before: int
    faces_after: int
    target_face_budget: int

    @property
    def simplification_degree(self) -> float:
        if self.faces_before == 0:
            return 0.0
        return 1.0 - float(self.faces_after) / float(self.faces_before)


def _nearest_indices(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    result = np.empty(source.shape[0], dtype=np.int64)
    for start in range(0, source.shape[0], 256):
        stop = min(start + 256, source.shape[0])
        difference = source[start:stop, None, :] - target[None, :, :]
        result[start:stop] = np.argmin(
            np.sum(difference * difference, axis=2), axis=1
        )
    return result


def fuse_meshes(
    geometry: WorldGeometry,
    transformed_observations: Iterable[tuple[str, np.ndarray]],
    *,
    fusion_strength: float,
    sigma: float,
) -> FusedMesh:
    """Fuse registered camera clouds with normalized per-vertex weights.

    Contributions are accumulated in camera-id and point-index order. This is
    intentionally serial: it makes repeated additions byte-for-byte stable.
    """

    observations = sorted(transformed_observations, key=lambda item: item[0])
    point_count = geometry.points.shape[0]
    weighted_sum = np.zeros((point_count, 3), dtype=np.float64)
    raw_weight_sums = np.zeros(point_count, dtype=np.float64)
    nearest_for_points: list[np.ndarray] = []
    weights_for_points: list[np.ndarray] = []
    sigma_value = max(float(sigma), float(np.finfo(np.float64).tiny))
    denominator = 2.0 * sigma_value * sigma_value

    for _camera_id, points in observations:
        cloud = np.asarray(points, dtype=np.float64)
        nearest = _nearest_indices(cloud, geometry.points)
        difference = cloud - geometry.points[nearest]
        squared = np.sum(difference * difference, axis=1)
        weights = np.exp(-squared / denominator)
        nearest_for_points.append(nearest)
        weights_for_points.append(weights)
        for point_index in range(cloud.shape[0]):
            vertex_index = int(nearest[point_index])
            weight = float(weights[point_index])
            raw_weight_sums[vertex_index] += weight
            weighted_sum[vertex_index] += weight * cloud[point_index]

    fused_vertices = geometry.points.copy()
    contributed = raw_weight_sums > 0.0
    if np.any(contributed):
        weighted_mean = np.zeros_like(fused_vertices)
        weighted_mean[contributed] = (
            weighted_sum[contributed] / raw_weight_sums[contributed, None]
        )
        fused_vertices[contributed] = (
            (1.0 - float(fusion_strength)) * geometry.points[contributed]
            + float(fusion_strength) * weighted_mean[contributed]
        )

    normalized_sums = np.zeros(point_count, dtype=np.float64)
    for nearest, weights in zip(nearest_for_points, weights_for_points):
        for point_index in range(weights.shape[0]):
            vertex_index = int(nearest[point_index])
            normalization = float(raw_weight_sums[vertex_index])
            if normalization > 0.0:
                normalized_sums[vertex_index] += float(weights[point_index]) / normalization

    errors = np.abs(normalized_sums[contributed] - 1.0)
    return FusedMesh(
        vertices=np.ascontiguousarray(fused_vertices, dtype=np.float64),
        faces=geometry.faces.copy(),
        uv=geometry.uv.copy(),
        raw_weight_sums=np.ascontiguousarray(raw_weight_sums, dtype=np.float64),
        normalized_weight_sums=np.ascontiguousarray(normalized_sums, dtype=np.float64),
        normalization_errors=np.ascontiguousarray(errors, dtype=np.float64),
        contributed_vertices=int(np.count_nonzero(contributed)),
    )


def _compact_mesh(
    vertices: np.ndarray,
    faces: np.ndarray,
    uv: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    used = np.unique(faces.reshape(-1)) if faces.size else np.empty(0, dtype=np.int64)
    if used.size == 0:
        return (
            np.empty((0, 3), dtype=np.float64),
            np.empty((0, 3), dtype=np.int64),
            np.empty((0, 2), dtype=np.float64),
        )
    remap = np.full(vertices.shape[0], -1, dtype=np.int64)
    remap[used] = np.arange(used.size, dtype=np.int64)
    return (
        np.ascontiguousarray(vertices[used], dtype=np.float64),
        np.ascontiguousarray(remap[faces], dtype=np.int64),
        np.ascontiguousarray(uv[used], dtype=np.float64),
    )


def _collapse_edge(
    vertices: np.ndarray,
    faces: np.ndarray,
    uv: np.ndarray,
    first: int,
    second: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    new_vertices = vertices.copy()
    new_uv = uv.copy()
    midpoint = 0.5 * (vertices[first] + vertices[second])
    new_vertices[first] = midpoint
    new_uv[first] = 0.5 * (uv[first] + uv[second])
    displacement = float(np.linalg.norm(midpoint - vertices[first]))

    remapped = faces.copy()
    remapped[remapped == second] = first
    keep = np.all(remapped[:, 0:1] != remapped[:, 1:2], axis=1)
    keep &= np.all(remapped[:, 1:2] != remapped[:, 2:3], axis=1)
    keep &= np.all(remapped[:, 0:1] != remapped[:, 2:3], axis=1)
    remapped = remapped[keep]

    seen: set[tuple[int, int, int]] = set()
    unique_faces: list[np.ndarray] = []
    for face in remapped:
        key = tuple(sorted(int(item) for item in face))
        if key in seen:
            continue
        seen.add(key)
        unique_faces.append(face)
    if unique_faces:
        remapped = np.ascontiguousarray(np.stack(unique_faces), dtype=np.int64)
    else:
        remapped = np.empty((0, 3), dtype=np.int64)
    return new_vertices, remapped, new_uv, displacement


def simplify_mesh(
    vertices: np.ndarray,
    faces: np.ndarray,
    uv: np.ndarray,
    target_face_budget: int,
    *,
    scene_id: str = "scene",
) -> MeshSimplificationResult:
    current_vertices = np.asarray(vertices, dtype=np.float64).copy()
    current_faces = np.asarray(faces, dtype=np.int64).copy()
    current_uv = np.asarray(uv, dtype=np.float64).copy()
    if current_vertices.ndim != 2 or current_vertices.shape[1] != 3:
        raise ValueError("vertices must have shape (N, 3)")
    if current_faces.ndim != 2 or current_faces.shape[1] != 3:
        raise ValueError("faces must have shape (M, 3)")
    if current_uv.shape != (current_vertices.shape[0], 2):
        raise ValueError("uv must have shape (N, 2)")
    budget = int(target_face_budget)
    if budget < 0:
        raise ValueError("target_face_budget must be >= 0")

    faces_before = int(current_faces.shape[0])
    history: list[dict[str, Any]] = []
    safety_counter = 0
    max_steps = max(faces_before * 4, 1)

    while int(current_faces.shape[0]) > budget and safety_counter < max_steps:
        safety_counter += 1
        if current_faces.shape[0] <= 1:
            break
        edge_sets = np.concatenate(
            (
                current_faces[:, [0, 1]],
                current_faces[:, [1, 2]],
                current_faces[:, [2, 0]],
            ),
            axis=0,
        )
        edge_sets = np.sort(edge_sets, axis=1)
        unique_edges = np.unique(edge_sets, axis=0)
        candidates: list[tuple[float, int, int]] = []
        for first, second in unique_edges:
            first_index = int(first)
            second_index = int(second)
            if first_index == second_index:
                continue
            delta = current_vertices[first_index] - current_vertices[second_index]
            cost = float(np.dot(delta, delta))
            candidates.append((cost, first_index, second_index))
        candidates.sort()

        chosen: tuple[int, np.ndarray, np.ndarray, np.ndarray, float] | None = None
        for cost, first, second in candidates:
            new_vertices, new_faces, new_uv, displacement = _collapse_edge(
                current_vertices, current_faces, current_uv, first, second
            )
            if new_faces.shape[0] < current_faces.shape[0]:
                chosen = (
                    first,
                    new_vertices,
                    new_faces,
                    new_uv,
                    displacement,
                )
                history.append(
                    {
                        "edge": [int(first), int(second)],
                        "cost": float(cost),
                        "displacement": float(displacement),
                        "faces_after": int(new_faces.shape[0]),
                    }
                )
                break
        if chosen is None:
            break
        _first, current_vertices, current_faces, current_uv, _displacement = chosen

    compact_vertices, compact_faces, compact_uv = _compact_mesh(
        current_vertices, current_faces, current_uv
    )
    faces_after = int(compact_faces.shape[0])
    if faces_after > budget:
        over_by = faces_after - budget
        raise FaceBudgetExceeded(
            scene_id=scene_id,
            over_budget_faces=over_by,
            faces_after=faces_after,
            target_face_budget=budget,
        )
    return MeshSimplificationResult(
        vertices=compact_vertices,
        faces=compact_faces,
        uv=compact_uv,
        history=tuple(history),
        faces_before=faces_before,
        faces_after=faces_after,
        target_face_budget=budget,
    )


def approximation_distances(
    original_vertices: np.ndarray,
    simplified_vertices: np.ndarray,
) -> np.ndarray:
    original = np.asarray(original_vertices, dtype=np.float64)
    simplified = np.asarray(simplified_vertices, dtype=np.float64)
    if simplified.shape[0] == 0:
        return np.full(original.shape[0], np.inf, dtype=np.float64)
    nearest = _nearest_indices(original, simplified)
    delta = original - simplified[nearest]
    return np.sqrt(np.sum(delta * delta, axis=1)).astype(np.float64, copy=False)

