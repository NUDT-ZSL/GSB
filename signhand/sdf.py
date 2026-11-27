"""World-space sphere, capsule and ellipsoid signed distance fields."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .math3d import inverse_rigid
from .project import PrimitiveSpec, Project


def _as_points(points) -> np.ndarray:
    array = np.asarray(points, dtype=np.float64)
    if array.ndim == 1:
        if array.shape != (3,):
            raise ValueError("a point must have shape (3,)")
        array = array.reshape(1, 3)
    if array.ndim != 2 or array.shape[1] != 3:
        raise ValueError("points must have shape (N, 3)")
    return array


@dataclass(frozen=True)
class PrimitiveInstance:
    """A primitive paired with its rigid world transform for one frame."""

    spec: PrimitiveSpec
    world_matrix: np.ndarray
    inverse_world_matrix: np.ndarray

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def bone(self) -> str:
        return self.spec.bone

    @property
    def color(self) -> np.ndarray:
        if self.spec.color is not None:
            return self.spec.color
        return np.array([0.78, 0.63, 0.52], dtype=np.float64)

    def _to_local(self, points: np.ndarray) -> np.ndarray:
        return points @ self.inverse_world_matrix[:3, :3].T + self.inverse_world_matrix[:3, 3]

    def distance(self, points) -> np.ndarray:
        points = _as_points(points)
        local = self._to_local(points)
        if self.spec.kind == "sphere":
            delta = local - self.spec.center
            return np.linalg.norm(delta, axis=1) - float(self.spec.radius)
        if self.spec.kind == "capsule":
            a = self.spec.a
            b = self.spec.b
            ba = b - a
            denominator = float(np.dot(ba, ba))
            h = np.clip(((local - a) @ ba) / denominator, 0.0, 1.0)
            closest = a + h[:, None] * ba
            return np.linalg.norm(local - closest, axis=1) - float(self.spec.radius)
        if self.spec.kind == "ellipsoid":
            radii = self.spec.radii
            normalized = (local - self.spec.center) / radii
            # A conservative radial SDF approximation. It underestimates the
            # exact Euclidean distance away from the minor axis, which keeps
            # sphere tracing conservative while remaining smooth and stable.
            return (np.linalg.norm(normalized, axis=1) - 1.0) * float(np.min(radii))
        raise ValueError(f"unsupported primitive type {self.spec.kind!r}")

    def surface_points(self, directions: np.ndarray) -> np.ndarray:
        directions = _as_points(directions)
        if self.spec.kind == "sphere":
            local = self.spec.center + directions * float(self.spec.radius)
        elif self.spec.kind == "capsule":
            a, b, radius = self.spec.a, self.spec.b, float(self.spec.radius)
            axis = b - a
            length = float(np.linalg.norm(axis))
            unit_axis = axis / max(length, 1e-12)
            # Project directions onto the plane perpendicular to the axis so
            # that samples form rings around the capsule barrel.
            projection = directions - (directions @ unit_axis)[:, None] * unit_axis
            norm = np.linalg.norm(projection, axis=1, keepdims=True)
            perpendicular = np.divide(
                projection,
                np.maximum(norm, 1e-12),
                out=np.tile(np.array([unit_axis[1], -unit_axis[0], 0.0]), (directions.shape[0], 1)),
                where=norm > 1e-9,
            )
            stations = np.linspace(0.0, 1.0, 5)[:, None]
            centers = a + stations * axis
            ring_angles = np.linspace(0.0, 2.0 * np.pi, max(directions.shape[0] // 5, 8), endpoint=False)
            basis_u = perpendicular[:1]
            basis_v = np.cross(unit_axis, basis_u)
            local = np.vstack(
                [
                    centers[i]
                    + radius
                    * (
                        np.cos(ring_angles)[:, None] * basis_u
                        + np.sin(ring_angles)[:, None] * basis_v
                    )
                    for i in range(centers.shape[0])
                ]
            )
        elif self.spec.kind == "ellipsoid":
            local = self.spec.center + directions * self.spec.radii
        else:
            raise ValueError(f"unsupported primitive type {self.spec.kind!r}")
        return local @ self.world_matrix[:3, :3].T + self.world_matrix[:3, 3]

    def aabb(self) -> tuple[np.ndarray, np.ndarray]:
        rotation = self.world_matrix[:3, :3]
        if self.spec.kind == "sphere":
            center = rotation @ self.spec.center + self.world_matrix[:3, 3]
            extent = np.full(3, float(self.spec.radius))
        elif self.spec.kind == "capsule":
            a = rotation @ self.spec.a + self.world_matrix[:3, 3]
            b = rotation @ self.spec.b + self.world_matrix[:3, 3]
            extent = np.full(3, float(self.spec.radius))
            return np.minimum(a, b) - extent, np.maximum(a, b) + extent
        elif self.spec.kind == "ellipsoid":
            center = rotation @ self.spec.center + self.world_matrix[:3, 3]
            axes = rotation @ np.diag(self.spec.radii)
            extent = np.linalg.norm(axes, axis=1)
        else:
            raise ValueError(f"unsupported primitive type {self.spec.kind!r}")
        return center - extent, center + extent


class SDFScene:
    """A deterministic union of transformed SDF primitives."""

    def __init__(
        self,
        primitives: tuple[PrimitiveInstance, ...] | list[PrimitiveInstance],
        smooth_union_radius: float = 0.0,
    ):
        if not primitives:
            raise ValueError("an SDF scene requires at least one primitive")
        self.primitives = tuple(primitives)
        self.primitive_map = {primitive.id: primitive for primitive in self.primitives}
        self.smooth_union_radius = float(smooth_union_radius)
        if self.smooth_union_radius < 0:
            raise ValueError("smooth union radius must be non-negative")
        if len(self.primitive_map) != len(self.primitives):
            raise ValueError("SDF primitive ids must be unique within a scene")

    @classmethod
    def from_project(cls, project: Project, world_matrices: dict[str, np.ndarray]) -> "SDFScene":
        instances = []
        for primitive in project.primitives:
            if primitive.bone not in world_matrices:
                raise ValueError(f"primitive {primitive.id} has no world transform for {primitive.bone}")
            world = world_matrices[primitive.bone]
            instances.append(PrimitiveInstance(primitive, world, inverse_rigid(world)))
        return cls(
            instances,
            smooth_union_radius=getattr(project.render, "smooth_union_radius", 0.0),
        )

    @classmethod
    def rest_scene(cls, project: Project, rest_world_matrices: dict[str, np.ndarray]) -> "SDFScene":
        return cls.from_project(project, rest_world_matrices)

    def subset(self, bones: set[str] | tuple[str, ...] | list[str]) -> "SDFScene":
        selected = set(bones)
        instances = [primitive for primitive in self.primitives if primitive.bone in selected]
        if not instances:
            raise ValueError("scene subset contains no primitives")
        return SDFScene(instances, smooth_union_radius=self.smooth_union_radius)

    def distance_matrix(self, points, primitives=None) -> np.ndarray:
        points = _as_points(points)
        selected = self.primitives if primitives is None else tuple(primitives)
        if not selected:
            raise ValueError("no primitives selected")
        return np.stack([primitive.distance(points) for primitive in selected], axis=1)

    def distance(self, points) -> np.ndarray:
        points = _as_points(points)
        values = self.distance_matrix(points)
        if self.smooth_union_radius <= 0.0 or values.shape[1] == 1:
            return np.min(values, axis=1)
        result = values[:, 0].copy()
        blend = self.smooth_union_radius
        for column in range(1, values.shape[1]):
            following = values[:, column]
            h = np.clip(0.5 + 0.5 * (following - result) / blend, 0.0, 1.0)
            result = following * (1.0 - h) + result * h - blend * h * (1.0 - h)
        return result

    def primitive_distance(self, points, primitive_id: str) -> np.ndarray:
        try:
            primitive = self.primitive_map[primitive_id]
        except KeyError as exc:
            raise KeyError(f"unknown SDF primitive {primitive_id!r}") from exc
        return primitive.distance(points)

    def pair_distance(self, points, first_id: str, second_id: str) -> tuple[np.ndarray, np.ndarray]:
        return (
            self.primitive_distance(points, first_id),
            self.primitive_distance(points, second_id),
        )

    def nearest_primitive(self, points) -> tuple[np.ndarray, np.ndarray]:
        points = _as_points(points)
        matrix = self.distance_matrix(points)
        indices = np.argmin(matrix, axis=1)
        names = np.asarray([primitive.id for primitive in self.primitives], dtype=object)
        minima = matrix[np.arange(points.shape[0]), indices]
        return names[indices], minima

    def gradient(self, points, epsilon: float = 8e-4) -> np.ndarray:
        points = _as_points(points)
        scale = float(epsilon)
        if scale <= 0:
            raise ValueError("gradient epsilon must be positive")
        offsets = np.eye(3, dtype=np.float64) * scale
        plus = np.stack([self.distance(points + offset) for offset in offsets], axis=1)
        minus = np.stack([self.distance(points - offset) for offset in offsets], axis=1)
        gradient = (plus - minus) / (2.0 * scale)
        norm = np.linalg.norm(gradient, axis=1, keepdims=True)
        return gradient / np.maximum(norm, 1e-12)

    def aabb(self) -> tuple[np.ndarray, np.ndarray]:
        boxes = [primitive.aabb() for primitive in self.primitives]
        return (
            np.min(np.stack([box[0] for box in boxes]), axis=0),
            np.max(np.stack([box[1] for box in boxes]), axis=0),
        )

    def ray_aabb_tmax(self, origins, directions) -> np.ndarray:
        origins = _as_points(origins)
        directions = _as_points(directions)
        if origins.shape != directions.shape:
            raise ValueError("ray origins and directions must have matching shapes")
        lower, upper = self.aabb()
        t_min = np.full(origins.shape[0], -np.inf, dtype=np.float64)
        t_max = np.full(origins.shape[0], np.inf, dtype=np.float64)
        for axis in range(3):
            origin = origins[:, axis]
            direction = directions[:, axis]
            safe = np.abs(direction) > 1e-14
            one = (lower[axis] - origin) / np.where(safe, direction, 1.0)
            two = (upper[axis] - origin) / np.where(safe, direction, 1.0)
            near_axis = np.minimum(one, two)
            far_axis = np.maximum(one, two)
            outside = (~safe) & ((origin < lower[axis]) | (origin > upper[axis]))
            t_min = np.where(safe, np.maximum(t_min, near_axis), t_min)
            t_max = np.where(safe, np.minimum(t_max, far_axis), t_max)
            t_max = np.where(outside, -np.inf, t_max)
        valid = t_max >= np.maximum(t_min, 0.0)
        return np.where(valid, t_max, np.inf)

