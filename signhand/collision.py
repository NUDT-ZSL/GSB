"""Deterministic cross-bone SDF overlap detection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .sdf import PrimitiveInstance, SDFScene


def _fibonacci_directions(count: int) -> np.ndarray:
    if count < 1:
        raise ValueError("sample count must be positive")
    index = np.arange(count, dtype=np.float64) + 0.5
    z = 1.0 - 2.0 * index / count
    radius = np.sqrt(np.maximum(1.0 - z * z, 0.0))
    angle = np.pi * (3.0 - np.sqrt(5.0)) * index
    return np.stack([np.cos(angle) * radius, np.sin(angle) * radius, z], axis=1)


@dataclass(frozen=True)
class CollisionCandidate:
    point: np.ndarray
    first_distance: float
    second_distance: float
    overlap_sdf: float

    @property
    def penetration_depth(self) -> float:
        return max(0.0, -self.overlap_sdf)


class IntersectionDetector:
    """Finds primitive-pair overlaps and the deepest point of each lens.

    For two primitive fields ``dA`` and ``dB``, their intersection has the
    conservative SDF ``max(dA, dB)``. Its negative region is exactly the
    region where both primitive interiors overlap, and its minimum supplies
    the reported maximum penetration depth.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        self.minimum_penetration = float(config.get("minimum_penetration", 1e-5))
        self.surface_samples = int(config.get("surface_samples", 192))
        self.grid_resolution = int(config.get("grid_resolution", 7))
        self.refinement_iterations = int(config.get("refinement_iterations", 32))
        if self.minimum_penetration < 0:
            raise ValueError("minimum_penetration must be non-negative")
        if self.surface_samples < 16 or self.grid_resolution < 2:
            raise ValueError("collision sampling configuration is too small")

    def _pair_candidates(
        self,
        first: PrimitiveInstance,
        second: PrimitiveInstance,
    ) -> tuple[np.ndarray | None, np.ndarray, np.ndarray]:
        first_lower, first_upper = first.aabb()
        second_lower, second_upper = second.aabb()
        lower = np.maximum(first_lower, second_lower)
        upper = np.minimum(first_upper, second_upper)
        if np.any(lower > upper):
            return None, lower, upper

        first_center = (first_lower + first_upper) * 0.5
        second_center = (second_lower + second_upper) * 0.5
        first_radius = float(np.linalg.norm(first_upper - first_lower) * 0.5)
        second_radius = float(np.linalg.norm(second_upper - second_lower) * 0.5)
        if np.linalg.norm(first_center - second_center) > first_radius + second_radius:
            return None, lower, upper

        grid_axes = [
            np.linspace(lower[axis], upper[axis], self.grid_resolution)
            for axis in range(3)
        ]
        grid = np.stack(np.meshgrid(*grid_axes, indexing="xy"), axis=-1).reshape(-1, 3)
        directions = _fibonacci_directions(self.surface_samples)
        surface_candidates = [
            first.surface_points(directions),
            second.surface_points(directions),
        ]
        if first.spec.kind == "capsule":
            radius = float(first.spec.radius)
            for endpoint in (first.spec.a, first.spec.b):
                world_endpoint = (
                    first.world_matrix[:3, :3] @ endpoint + first.world_matrix[:3, 3]
                )
                surface_candidates.append(world_endpoint + directions * radius)
        if second.spec.kind == "capsule":
            radius = float(second.spec.radius)
            for endpoint in (second.spec.a, second.spec.b):
                world_endpoint = (
                    second.world_matrix[:3, :3] @ endpoint + second.world_matrix[:3, 3]
                )
                surface_candidates.append(world_endpoint + directions * radius)

        line_count = 24
        line = np.linspace(first_center, second_center, line_count)
        anchors = np.vstack(
            [
                first_center[None, :],
                second_center[None, :],
                (first_center + second_center)[None, :] * 0.5,
                line,
            ]
        )
        candidates = np.vstack([grid, *surface_candidates, anchors])
        return candidates, lower, upper

    def _deepest_overlap(
        self,
        first: PrimitiveInstance,
        second: PrimitiveInstance,
        candidates: np.ndarray,
        lower: np.ndarray,
        upper: np.ndarray,
    ) -> CollisionCandidate | None:
        if candidates is None or candidates.shape[0] == 0:
            return None
        first_distance = first.distance(candidates)
        second_distance = second.distance(candidates)
        overlap = (first_distance < 0.0) & (second_distance < 0.0)
        if not np.any(overlap):
            return None
        scores = np.maximum(first_distance, second_distance)
        best_index = int(np.argmin(np.where(overlap, scores, np.inf)))
        best_point = candidates[best_index].copy()
        best_score = float(scores[best_index])

        extent = np.maximum(upper - lower, 1e-6)
        step = extent * 0.5

        def evaluate(points: np.ndarray):
            d_first = first.distance(points)
            d_second = second.distance(points)
            valid = (d_first < 0.0) & (d_second < 0.0)
            score = np.maximum(d_first, d_second)
            objective = np.where(valid, score, 1e6 + np.maximum(d_first, d_second))
            return objective, d_first, d_second, valid

        for _ in range(self.refinement_iterations):
            neighbors = np.vstack(
                [
                    best_point,
                    best_point + np.array([step[0], 0.0, 0.0]),
                    best_point - np.array([step[0], 0.0, 0.0]),
                    best_point + np.array([0.0, step[1], 0.0]),
                    best_point - np.array([0.0, step[1], 0.0]),
                    best_point + np.array([0.0, 0.0, step[2]]),
                    best_point - np.array([0.0, 0.0, step[2]]),
                ]
            )
            neighbors = np.clip(neighbors, lower, upper)
            objective, d_first, d_second, valid = evaluate(neighbors)
            index = int(np.argmin(objective))
            if valid[index] and objective[index] < best_score - 1e-12:
                best_score = float(objective[index])
                best_point = neighbors[index].copy()
            else:
                step *= 0.5
            if np.all(step < 1e-8):
                break

        d_first = float(first.distance(best_point[None, :])[0])
        d_second = float(second.distance(best_point[None, :])[0])
        if d_first >= 0.0 or d_second >= 0.0:
            return None
        score = max(d_first, d_second)
        if -score < self.minimum_penetration:
            return None
        return CollisionCandidate(best_point, d_first, d_second, score)

    def detect(self, scene: SDFScene, frame: int, label: str = "") -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        primitives = scene.primitives
        for first_index in range(len(primitives)):
            first = primitives[first_index]
            for second_index in range(first_index + 1, len(primitives)):
                second = primitives[second_index]
                if first.bone == second.bone:
                    continue
                candidates, lower, upper = self._pair_candidates(first, second)
                if candidates is None:
                    continue
                deepest = self._deepest_overlap(
                    first, second, candidates, lower, upper
                )
                if deepest is None:
                    continue
                primitive_ids = sorted([first.id, second.id])
                records.append(
                    {
                        "frame": int(frame),
                        "moment": label,
                        "primitive_a": primitive_ids[0],
                        "primitive_b": primitive_ids[1],
                        "bone_a": (
                            first.bone
                            if primitive_ids[0] == first.id
                            else second.bone
                        ),
                        "bone_b": (
                            second.bone
                            if primitive_ids[0] == first.id
                            else first.bone
                        ),
                        "max_penetration_depth": float(deepest.penetration_depth),
                        "overlap_sdf_min": float(-deepest.penetration_depth),
                        "overlap_point": [float(value) for value in deepest.point],
                        "primitive_a_distance": float(
                            deepest.first_distance
                            if primitive_ids[0] == first.id
                            else deepest.second_distance
                        ),
                        "primitive_b_distance": float(
                            deepest.second_distance
                            if primitive_ids[0] == first.id
                            else deepest.first_distance
                        ),
                    }
                )
        records.sort(
            key=lambda item: (
                item["frame"],
                item["bone_a"],
                item["bone_b"],
                item["primitive_a"],
                item["primitive_b"],
            )
        )
        return records

