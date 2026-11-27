"""Post-skinning surface-distance and approximate-volume verification."""

from __future__ import annotations

from typing import Any

import numpy as np

from .animation import SkinResult, SkinnedHand
from .project import Project
from .sdf import SDFScene


def _mesh_volume(vertices: np.ndarray, triangles: np.ndarray) -> float:
    if triangles.shape[0] == 0:
        return 0.0
    first = vertices[triangles[:, 0]]
    second = vertices[triangles[:, 1]]
    third = vertices[triangles[:, 2]]
    signed = np.einsum("ij,ij->i", first, np.cross(second, third)) / 6.0
    return float(abs(np.sum(signed)))


class DeformationAuditor:
    """Measures LBS drift relative to the rest-pose shell of selected bones."""

    def __init__(
        self,
        project: Project,
        skinner: SkinnedHand,
        rest_scene: SDFScene,
        config: dict[str, Any] | None = None,
    ):
        self.project = project
        self.skinner = skinner
        self.rest_scene = rest_scene
        config = config or project.deformation_config
        self.shrink_ratio = float(config.get("shrink_volume_ratio", 0.08))
        self.expand_ratio = float(config.get("expand_volume_ratio", 0.12))
        self.surface_tolerance = float(config.get("surface_distance_tolerance", 0.012))
        self.quantile = float(config.get("surface_error_quantile", 0.95))
        if not 0.0 <= self.shrink_ratio < 1.0:
            raise ValueError("shrink_volume_ratio must be in [0, 1)")
        if self.expand_ratio <= 0.0:
            raise ValueError("expand_volume_ratio must be positive")
        if self.surface_tolerance <= 0.0 or not 0.0 < self.quantile <= 1.0:
            raise ValueError("deformation surface thresholds are out of range")

        self.bones = tuple(project.verification_bones)
        if not self.bones:
            self.bones = tuple(
                name for name in project.joint_order if name != project.joint_order[0]
            )
        self._vertex_masks = {
            bone: np.asarray(
                [part == bone for part in project.mesh.vertex_parts],
                dtype=bool,
            )
            for bone in self.bones
        }
        self._triangle_masks = {
            bone: np.asarray(
                [part == bone for part in project.mesh.triangle_parts],
                dtype=bool,
            )
            for bone in self.bones
        }
        for bone in self.bones:
            if bone not in project.joint_map:
                raise ValueError(f"deformation verification references unknown bone {bone!r}")
            if not np.any(self._vertex_masks[bone]):
                raise ValueError(f"deformation verification bone {bone!r} has no mesh part")
            if not np.any(self._triangle_masks[bone]):
                raise ValueError(f"deformation verification bone {bone!r} has no triangles")

    def _thresholds(self) -> dict[str, Any]:
        return {
            "volume_ratio_lower_bound": 1.0 - self.shrink_ratio,
            "volume_ratio_upper_bound": 1.0 + self.expand_ratio,
            "surface_distance_tolerance": self.surface_tolerance,
            "surface_distance_quantile": self.quantile,
        }

    def audit_frame(
        self,
        frame: int,
        label: str,
        skin_result: SkinResult,
        posed_scene: SDFScene,
    ) -> dict[str, Any]:
        segments: list[dict[str, Any]] = []
        verification_vertices = self.project.mesh.vertices
        skinned_vertices = skin_result.skinned_vertices
        rest_bind = self.skinner.sampler.inverse_bind_matrices
        triangles = self.project.mesh.triangles

        for bone in self.bones:
            vertex_mask = self._vertex_masks[bone]
            triangle_mask = self._triangle_masks[bone]
            part_triangles = triangles[triangle_mask]
            rest_points = verification_vertices[vertex_mask]
            posed_points = skinned_vertices[vertex_mask]
            bone_world = skin_result.world_matrices[bone]
            rigid_points = (
                np.concatenate(
                    [rest_points, np.ones((rest_points.shape[0], 1))], axis=1
                )
                @ (bone_world @ rest_bind[bone]).T
            )[:, :3]

            rest_bone_scene = self.rest_scene.subset([bone])
            posed_bone_scene = posed_scene.subset([bone])
            rest_distance = rest_bone_scene.distance(rest_points)
            posed_distance = posed_bone_scene.distance(posed_points)
            rigid_distance = posed_bone_scene.distance(rigid_points)
            absolute_distance = np.abs(posed_distance)
            quantile_distance = float(np.quantile(absolute_distance, self.quantile))
            max_absolute_distance = float(np.max(absolute_distance))

            rest_volume = _mesh_volume(verification_vertices, part_triangles)
            skinned_volume = _mesh_volume(skinned_vertices, part_triangles)
            ratio = (
                skinned_volume / rest_volume
                if rest_volume > 1e-14
                else float("nan")
            )
            rest_triangle = verification_vertices[part_triangles]
            posed_triangle = skinned_vertices[part_triangles]
            rest_normals = np.cross(
                rest_triangle[:, 1] - rest_triangle[:, 0],
                rest_triangle[:, 2] - rest_triangle[:, 0],
            )
            expected_normals = rest_normals @ bone_world[:3, :3].T
            posed_normals = np.cross(
                posed_triangle[:, 1] - posed_triangle[:, 0],
                posed_triangle[:, 2] - posed_triangle[:, 0],
            )
            inverted_triangles = int(
                np.sum(np.einsum("ij,ij->i", expected_normals, posed_normals) < 0.0)
            )

            flags: list[str] = []
            if np.isfinite(ratio) and ratio < 1.0 - self.shrink_ratio:
                flags.append("VOLUME_SHRINK")
            if np.isfinite(ratio) and ratio > 1.0 + self.expand_ratio:
                flags.append("ABNORMAL_EXPANSION")
            if quantile_distance > self.surface_tolerance:
                flags.append("SDF_SURFACE_DRIFT")
            if inverted_triangles > 0:
                flags.append("TRIANGLE_INVERSION")

            segments.append(
                {
                    "bone": bone,
                    "sample_count": int(rest_points.shape[0]),
                    "rest_volume": rest_volume,
                    "skinned_volume": skinned_volume,
                    "volume_ratio": ratio,
                    "rest_sdf_distance_mean": float(np.mean(rest_distance)),
                    "rest_sdf_distance_max_abs": float(np.max(np.abs(rest_distance))),
                    "skinned_sdf_distance_mean": float(np.mean(posed_distance)),
                    "skinned_sdf_distance_p95_abs": quantile_distance,
                    "skinned_sdf_distance_max_abs": max_absolute_distance,
                    "rigid_reference_sdf_distance_mean": float(np.mean(rigid_distance)),
                    "lbs_rigid_vertex_rms": float(
                        np.sqrt(np.mean(np.sum((posed_points - rigid_points) ** 2, axis=1)))
                    ),
                    "inverted_triangle_count": inverted_triangles,
                    "flags": flags,
                    "status": "REVIEW" if flags else "PASS",
                }
            )

        flagged = int(sum(segment["status"] == "REVIEW" for segment in segments))
        return {
            "frame": int(frame),
            "moment": label,
            "segments": segments,
            "flagged_segment_count": flagged,
        }

    def report(self, frames: list[dict[str, Any]]) -> dict[str, Any]:
        flagged = int(sum(frame["flagged_segment_count"] for frame in frames))
        return {
            "schema_version": "1.0",
            "project_id": self.project.project_id,
            "project_version": self.project.project_version,
            "method": (
                "Compare rest-pose and LBS surface samples against their respective "
                "bone-local SDF shells. Approximate volume uses the closed triangle "
                "part mesh at rest and after linear blend skinning."
            ),
            "thresholds": self._thresholds(),
            "decision_rule": (
                "REVIEW when volume_ratio is outside the bounds, the configured "
                "quantile of |posed SDF distance| exceeds the surface tolerance, "
                "or any triangle orientation reverses."
            ),
            "summary": {
                "checked_frames": len(frames),
                "checked_segments": sum(len(frame["segments"]) for frame in frames),
                "flagged_segments": flagged,
                "status": "REVIEW" if flagged else "PASS",
            },
            "frames": frames,
        }

