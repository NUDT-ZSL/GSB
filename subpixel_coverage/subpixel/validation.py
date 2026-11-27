"""Project loading and curve consistency validation."""
from __future__ import annotations

import math
from typing import List, Tuple

from .generator import read_project
from .models import Curve, CurveIssue, PackagingProject


def _outline_extent(project: PackagingProject, surface_name: str) -> Tuple[float, float, float, float]:
    if surface_name == project.body.name:
        u0, _v0, u1, _v1 = project.outline_bounds
        return u0, project.body.y_min, u1, project.body.y_max
    u0, u1 = project.cap.theta_min * project.cap.radius, project.cap.theta_max * project.cap.radius
    return u0, project.cap.y_min, u1, project.cap.y_max


def validate_curve(curve: Curve, project: PackagingProject) -> List[CurveIssue]:
    issues: List[CurveIssue] = []
    u_min, v_min, u_max, v_max = _outline_extent(project, curve.surface)

    u, v = curve.start_2d
    if not (u_min - 1e-9 <= u <= u_max + 1e-9 and v_min - 1e-9 <= v <= v_max + 1e-9):
        issues.append(CurveIssue(
            curve.curve_id, "E_START_OUTSIDE",
            f"start {curve.start_2d} outside outline extent "
            f"({u_min:.3f},{v_min:.3f})-({u_max:.3f},{v_max:.3f})",
        ))

    norm = math.hypot(*curve.tangent_2d)
    if abs(norm - 1.0) > 1e-6:
        issues.append(CurveIssue(
            curve.curve_id, "E_TANGENT_NORM",
            f"tangent norm {norm:.6f} is not unit length",
        ))

    arc_sum = 0.0
    for a, b in zip(curve.points_3d, curve.points_3d[1:]):
        arc_sum += math.sqrt(sum((b[i] - a[i]) ** 2 for i in range(3)))
    point_outside = any(
        issue.code == "E_POINT_OUTSIDE" for issue in issues
    )
    if not point_outside and abs(arc_sum - curve.declared_length) > 1e-3:
        issues.append(CurveIssue(
            curve.curve_id, "E_LENGTH_MISMATCH",
            f"polyline arc {arc_sum:.6f} != declared length "
            f"{curve.declared_length:.6f}",
        ))

    surface = project.surface_by_name(curve.surface)
    for index, point in enumerate(curve.points_3d):
        radial = math.hypot(point[0], point[2])
        if abs(radial - surface.radius) > 1e-5:
            issues.append(CurveIssue(
                curve.curve_id, "E_POINT_OUTSIDE",
                f"polyline vertex {index} radial distance {radial:.6f} "
                f"!= surface radius {surface.radius:.6f}",
            ))
            break
        if not (surface.y_min - 1e-9 <= point[1] <= surface.y_max + 1e-9):
            issues.append(CurveIssue(
                curve.curve_id, "E_POINT_OUTSIDE",
                f"polyline vertex {index} height {point[1]:.6f} outside "
                f"surface range [{surface.y_min},{surface.y_max}]",
            ))
            break

    if not issues and curve.points_3d:
        first = curve.points_3d[0]
        ref = surface.point(u / surface.radius, v)
        if any(abs(first[i] - ref[i]) > 1e-6 for i in range(3)):
            issues.append(CurveIssue(
                curve.curve_id, "E_START_MISMATCH",
                "polyline first vertex does not match declared outline start",
            ))
    return issues


def validate_project(project: PackagingProject) -> PackagingProject:
    """Populate ``project.issues`` with every inconsistent curve."""
    seen: set[str] = set()
    issues: List[CurveIssue] = []
    for curve in project.curves:
        for issue in validate_curve(curve, project):
            key = (issue.curve_id, issue.code)
            if key not in seen:
                seen.add(key)
                issues.append(issue)
    project.issues = issues
    return project


def load_project(path: str) -> PackagingProject:
    project = read_project(path)
    return validate_project(project)
