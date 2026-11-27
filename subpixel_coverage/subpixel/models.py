"""Domain models for a deterministic brand packaging showcase project."""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Tuple

import numpy as np


@dataclass
class Camera:
    """Simple pinhole camera looking at the origin region of the scene."""

    name: str
    position: Tuple[float, float, float]
    target: Tuple[float, float, float]
    up_hint: Tuple[float, float, float]
    focal_px: float
    width_px: int
    height_px: int

    def frame(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        forward = np.subtract(self.target, self.position, dtype=np.float64)
        forward /= np.linalg.norm(forward)
        right = np.cross(forward, np.asarray(self.up_hint, dtype=np.float64))
        right /= np.linalg.norm(right)
        up = np.cross(right, forward)
        return forward, right, up

    def project(self, points: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Project 3D points to pixel coordinates.

        Returns ``(xy, depth)`` where ``xy`` has shape ``(N, 2)`` with pixel
        coordinates whose origin is the top-left corner of pixel ``(0, 0)``.
        """
        pts = np.asarray(points, dtype=np.float64)
        forward, right, up = self.frame()
        rel = pts - np.asarray(self.position, dtype=np.float64)
        depth = rel @ forward
        x_cam = rel @ right
        y_cam = rel @ up
        safe_depth = np.where(np.abs(depth) < 1e-12, 1e-12, depth)
        u = self.width_px * 0.5 + self.focal_px * x_cam / safe_depth
        v = self.height_px * 0.5 - self.focal_px * y_cam / safe_depth
        return np.column_stack([u, v]), depth

    def scaled(self, width_px: int, height_px: int) -> "Camera":
        scale = width_px / self.width_px
        return Camera(
            name=f"{self.name}@{width_px}x{height_px}",
            position=self.position,
            target=self.target,
            up_hint=self.up_hint,
            focal_px=self.focal_px * scale,
            width_px=width_px,
            height_px=height_px,
        )


@dataclass
class DieLine:
    """A packaging die-line polygon in unwrapped outline coordinates."""

    name: str
    material: str
    polygon: List[Tuple[float, float]]


@dataclass
class Surface:
    """Parametric cylindrical surface (bottle body or cap)."""

    name: str
    material: str
    radius: float
    y_min: float
    y_max: float
    theta_min: float
    theta_max: float

    def point(self, theta: float, y: float) -> Tuple[float, float, float]:
        return (self.radius * math.sin(theta), y, self.radius * math.cos(theta))

    def normal(self, theta: float) -> Tuple[float, float, float]:
        return (math.sin(theta), 0.0, math.cos(theta))


@dataclass
class LabelUV:
    """Label artwork mapping from outline coordinates to surface parameters."""

    name: str
    outline_rect: Tuple[float, float, float, float]
    theta_range: Tuple[float, float]
    y_range: Tuple[float, float]

    def map_outline(self, u: float, v: float) -> Tuple[float, float]:
        u0, v0, u1, v1 = self.outline_rect
        t0, t1 = self.theta_range
        y0, y1 = self.y_range
        s = (u - u0) / (u1 - u0)
        t = (v - v0) / (v1 - v0)
        return t0 + s * (t1 - t0), y0 + t * (y1 - y0)


@dataclass
class Light:
    """Studio light with a soft-box style angular spread."""

    role: str
    direction: Tuple[float, float, float]
    intensity: float
    color_temp_k: float
    softness: float


@dataclass
class Curve:
    """One fibre or sandblast grain, defined in outline coordinates.

    ``start_2d`` and ``tangent_2d`` live in the packaging outline coordinate
    system.  ``points_3d`` is the evaluated 3D polyline on its host surface.
    """

    curve_id: str
    kind: str
    material: str
    start_2d: Tuple[float, float]
    tangent_2d: Tuple[float, float]
    declared_length: float
    radius_px: float
    opacity: float
    surface: str
    points_3d: List[Tuple[float, float, float]]
    rim_guard: bool = False


@dataclass
class CurveIssue:
    """A consistency failure detected while validating a curve."""

    curve_id: str
    code: str
    message: str


@dataclass
class PackagingProject:
    project_id: str
    seed: int
    version: str
    outline_bounds: Tuple[float, float, float, float]
    body: Surface
    cap: Surface
    die_lines: List[DieLine]
    labels: List[LabelUV]
    lights: List[Light]
    camera: Camera
    curves: List[Curve]
    material_of_surface: Dict[str, str]
    issues: List[CurveIssue] = field(default_factory=list)

    def surface_by_name(self, name: str) -> Surface:
        if name == self.body.name:
            return self.body
        if name == self.cap.name:
            return self.cap
        raise KeyError(name)

    def valid_curves(self) -> List[Curve]:
        invalid = {issue.curve_id for issue in self.issues}
        return [curve for curve in self.curves if curve.curve_id not in invalid]

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PackagingProject":
        data = dict(data)
        data["outline_bounds"] = tuple(data["outline_bounds"])
        data["body"] = Surface(**data["body"])
        data["cap"] = Surface(**data["cap"])
        data["die_lines"] = [DieLine(**item) for item in data["die_lines"]]
        data["labels"] = [LabelUV(**item) for item in data["labels"]]
        data["lights"] = [Light(**item) for item in data["lights"]]
        data["camera"] = Camera(**data["camera"])
        data["curves"] = [Curve(**item) for item in data["curves"]]
        data["issues"] = [CurveIssue(**item) for item in data.get("issues", [])]
        return cls(**data)


def summary(project: PackagingProject) -> Dict[str, Any]:
    """Human and machine readable project summary."""
    counts: Dict[str, int] = {}
    material_counts: Dict[str, int] = {}
    for curve in project.curves:
        counts[curve.kind] = counts.get(curve.kind, 0) + 1
        material_counts[curve.material] = material_counts.get(curve.material, 0) + 1
    invalid = sorted({issue.curve_id for issue in project.issues})
    return {
        "project_id": project.project_id,
        "seed": project.seed,
        "version": project.version,
        "outline_bounds": list(project.outline_bounds),
        "resolution": [project.camera.width_px, project.camera.height_px],
        "surfaces": {
            "body": asdict(project.body),
            "cap": asdict(project.cap),
        },
        "die_lines": [line.name for line in project.die_lines],
        "labels": [label.name for label in project.labels],
        "lights": {light.role: light.intensity for light in project.lights},
        "curve_total": len(project.curves),
        "curve_by_kind": counts,
        "curve_by_material": material_counts,
        "invalid_curve_count": len(invalid),
        "invalid_curve_ids": invalid,
        "issues": [asdict(issue) for issue in project.issues],
    }

