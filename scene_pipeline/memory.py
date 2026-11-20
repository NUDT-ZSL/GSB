"""Shared resident-memory and registration-comparison admission arbitration.

Geometry intermediates and baked textures compete for one resident-memory
budget.  The arbiter is deterministic: explicit downgrade levers are applied
in their configured order until the scene fits, and every accepted/rejected
decision records the arbitration path so scheduler reports are reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from .geometry import estimate_geometry_units
from .texture import estimate_texture_units


@dataclass
class SceneResourceRequest:
    scene_id: str
    priority: int
    face_budget: int
    vertex_estimate: int
    texture_resolution: int
    camera_count: int
    point_count: int
    icp_iterations: int


@dataclass
class AdmissionDecision:
    scene_id: str
    admitted: bool
    rejection_code: str
    final_face_budget: int
    final_texture_resolution: int
    final_icp_iterations: int
    geometry_units: int
    texture_units: int
    total_units: int
    memory_budget: int
    comparison_budget: int
    planned_comparisons: int
    downgrade_categories: List[str] = field(default_factory=list)
    arbitration_path: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "admitted": self.admitted,
            "rejection_code": self.rejection_code,
            "final_face_budget": int(self.final_face_budget),
            "final_texture_resolution": int(self.final_texture_resolution),
            "final_icp_iterations": int(self.final_icp_iterations),
            "geometry_units": int(self.geometry_units),
            "texture_units": int(self.texture_units),
            "total_units": int(self.total_units),
            "memory_budget": int(self.memory_budget),
            "comparison_budget": int(self.comparison_budget),
            "planned_comparisons": int(self.planned_comparisons),
            "downgrade_categories": list(self.downgrade_categories),
            "arbitration_path": list(self.arbitration_path),
        }


def _geometry_units(request: SceneResourceRequest) -> int:
    # One resident fused/simplified mesh at a time; vertex count is bounded by
    # the occupied-voxel estimate and face count by the active budget.
    return estimate_geometry_units(
        face_count=request.face_budget,
        vertex_count=request.vertex_estimate,
    )


def _planned_comparisons(request: SceneResourceRequest) -> int:
    passes = request.icp_iterations + 1
    return int(request.camera_count * request.point_count * passes)


def _downgrade(request: SceneResourceRequest, lever: str) -> SceneResourceRequest:
    data = request.__dict__.copy()
    if lever == "texture_resolution":
        data["texture_resolution"] = max(4, request.texture_resolution // 2)
    elif lever == "face_budget":
        data["face_budget"] = max(4, request.face_budget // 2)
    elif lever == "icp_iterations":
        data["icp_iterations"] = max(2, request.icp_iterations - 2)
    else:
        raise ValueError(f"Unknown downgrade lever: {lever}")
    return SceneResourceRequest(**data)


def arbitrate(request: SceneResourceRequest, memory_budget: int,
              comparison_budget: int,
              downgrade_order: tuple) -> AdmissionDecision:
    """Return a deterministic admission decision for one scene."""
    current = request
    path: List[str] = [
        f"initial face_budget={request.face_budget} "
        f"texture_resolution={request.texture_resolution} "
        f"icp_iterations={request.icp_iterations}"
    ]
    downgraded: List[str] = []

    while True:
        geometry_units = _geometry_units(current)
        texture_units = estimate_texture_units(current.texture_resolution)
        total_units = geometry_units + texture_units
        planned_comparisons = _planned_comparisons(current)
        memory_fits = total_units <= memory_budget
        comparison_fits = planned_comparisons <= comparison_budget

        if memory_fits and comparison_fits:
            return AdmissionDecision(
                scene_id=current.scene_id,
                admitted=True,
                rejection_code="",
                final_face_budget=current.face_budget,
                final_texture_resolution=current.texture_resolution,
                final_icp_iterations=current.icp_iterations,
                geometry_units=geometry_units,
                texture_units=texture_units,
                total_units=total_units,
                memory_budget=memory_budget,
                comparison_budget=comparison_budget,
                planned_comparisons=planned_comparisons,
                downgrade_categories=downgraded,
                arbitration_path=path,
            )

        progressed = False
        for lever in downgrade_order:
            candidate = _downgrade(current, lever)
            if candidate == current:
                continue
            current = candidate
            if lever not in downgraded:
                downgraded.append(lever)
            path.append(
                f"downgrade {lever} -> face_budget={current.face_budget} "
                f"texture_resolution={current.texture_resolution} "
                f"icp_iterations={current.icp_iterations}"
            )
            progressed = True
            break
        if progressed:
            continue

        rejection_code = (
            "E_MEMORY_BUDGET" if not memory_fits else "E_REG_BUDGET"
        )
        path.append(f"reject {rejection_code}")
        return AdmissionDecision(
            scene_id=current.scene_id,
            admitted=False,
            rejection_code=rejection_code,
            final_face_budget=current.face_budget,
            final_texture_resolution=current.texture_resolution,
            final_icp_iterations=current.icp_iterations,
            geometry_units=geometry_units,
            texture_units=texture_units,
            total_units=total_units,
            memory_budget=memory_budget,
            comparison_budget=comparison_budget,
            planned_comparisons=planned_comparisons,
            downgrade_categories=downgraded,
            arbitration_path=path,
        )


def build_request(manifest, point_count: int, vertex_estimate: int,
                  texture_resolution: int,
                  icp_iterations: int) -> SceneResourceRequest:
    return SceneResourceRequest(
        scene_id=manifest.scene_id,
        priority=manifest.priority,
        face_budget=manifest.target_face_budget,
        vertex_estimate=vertex_estimate,
        texture_resolution=texture_resolution,
        camera_count=len(manifest.camera_poses),
        point_count=point_count,
        icp_iterations=icp_iterations,
    )
