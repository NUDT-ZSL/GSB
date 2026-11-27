"""Forward-kinematics sampling and linear blend skinning."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .math3d import euler_xyz_matrix, inverse_rigid, transform
from .project import Project


def _joint_local_matrix(project: Project, name: str, frame: float) -> np.ndarray:
    joint = project.joint_map[name]
    joint_tracks = project.tracks.get(name)
    animation_translation = (
        np.zeros(3, dtype=np.float64)
        if joint_tracks is None
        else joint_tracks.translation_at(frame)
    )
    animation_rotation = (
        np.zeros(3, dtype=np.float64)
        if joint_tracks is None
        else joint_tracks.rotation_at(frame)
    )
    rest_rotation = euler_xyz_matrix(joint.rest_rotation_euler_deg)
    return transform(
        rotation=rest_rotation @ euler_xyz_matrix(animation_rotation),
        translation=joint.rest_translation + animation_translation,
    )


class AnimationSampler:
    """Evaluates the skeleton hierarchy at fractional or integer frames."""

    def __init__(self, project: Project):
        self.project = project
        self.rest_world_matrices = self._rest_world()
        self.inverse_bind_matrices = {
            name: inverse_rigid(matrix) for name, matrix in self.rest_world_matrices.items()
        }

    def _rest_world(self) -> dict[str, np.ndarray]:
        result: dict[str, np.ndarray] = {}
        for name in self.project.joint_order:
            joint = self.project.joint_map[name]
            local = transform(
                rotation=euler_xyz_matrix(joint.rest_rotation_euler_deg),
                translation=joint.rest_translation,
            )
            if joint.parent is None:
                result[name] = local
            else:
                result[name] = result[joint.parent] @ local
        return result

    def sample(self, frame: float) -> dict[str, np.ndarray]:
        frame = float(frame)
        if not np.isfinite(frame):
            raise ValueError("frame must be finite")
        result: dict[str, np.ndarray] = {}
        for name in self.project.joint_order:
            local = _joint_local_matrix(self.project, name, frame)
            parent = self.project.joint_map[name].parent
            result[name] = local if parent is None else result[parent] @ local
        return result

    def world_matrix_array(self, frame: float) -> np.ndarray:
        matrices = self.sample(frame)
        return np.stack([matrices[name] for name in self.project.joint_order])

    def rest_matrix_array(self) -> np.ndarray:
        return np.stack([self.rest_world_matrices[name] for name in self.project.joint_order])


@dataclass(frozen=True)
class SkinResult:
    frame: int
    time_seconds: float
    world_matrices: dict[str, np.ndarray]
    skin_matrices: dict[str, np.ndarray]
    skinned_vertices: np.ndarray


@dataclass(frozen=True)
class PoseSample:
    frame: int
    time_seconds: float
    world_matrices: dict[str, np.ndarray]


class SkinnedHand:
    """Vectorized linear blend skinning over the project's bind-pose mesh."""

    def __init__(self, project: Project, sampler: AnimationSampler | None = None):
        self.project = project
        self.sampler = sampler or AnimationSampler(project)
        self._weights = project.mesh.weight_matrix
        self._vertices_h = np.concatenate(
            [project.mesh.vertices, np.ones((project.mesh.vertices.shape[0], 1))],
            axis=1,
        )

    def frame_time(self, frame: int | float) -> float:
        return (float(frame) - float(self.project.frame_start)) / self.project.fps

    def skin_with_world(
        self,
        frame: int | float,
        world_matrices: dict[str, np.ndarray],
    ) -> SkinResult:
        skin_matrices: dict[str, np.ndarray] = {}
        output = np.zeros_like(self.project.mesh.vertices)
        for column, name in enumerate(self.project.joint_order):
            weights = self._weights[:, column]
            active = weights > 0.0
            if not np.any(active):
                skin_matrices[name] = np.eye(4, dtype=np.float64)
                continue
            matrix = world_matrices[name] @ self.sampler.inverse_bind_matrices[name]
            skin_matrices[name] = matrix
            transformed = self._vertices_h[active] @ matrix.T
            output[active] += transformed[:, :3] * weights[active, None]
        return SkinResult(
            frame=int(frame),
            time_seconds=self.frame_time(frame),
            world_matrices=world_matrices,
            skin_matrices=skin_matrices,
            skinned_vertices=output,
        )

    def skin(self, frame: int | float) -> SkinResult:
        world = self.sampler.sample(float(frame))
        return self.skin_with_world(frame, world)

    def rest_skinned_vertices(self) -> np.ndarray:
        return self.project.mesh.vertices.copy()

    def rigid_part_vertices(self, bone: str, frame: int | float, world=None) -> np.ndarray:
        if bone not in self.project.joint_map:
            raise ValueError(f"unknown bone {bone!r}")
        world = self.sampler.sample(float(frame)) if world is None else world
        mask = np.asarray([part == bone for part in self.project.mesh.vertex_parts])
        rigid = (
            self._vertices_h[mask]
            @ (world[bone] @ self.sampler.inverse_bind_matrices[bone]).T
        )
        return rigid[:, :3]


# A descriptive alias for callers that refer to the whole animation evaluator.
AnimationSamplerWithSkin = SkinnedHand
PoseSolver = AnimationSampler

