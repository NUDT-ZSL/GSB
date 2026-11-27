"""Deterministic packaging project generator.

A fixed NumPy ``Generator`` seed produces every geometry, fibre and grain, so
the generated showcase project and all downstream coverage results are fully
reproducible.
"""
from __future__ import annotations

import json
import math
import os
from typing import List, Tuple

import numpy as np

from .models import Camera, Curve, DieLine, LabelUV, Light, PackagingProject, Surface

SEED = 20261128
PROJECT_ID = "frosted-bottle-showcase"
ENGINE_VERSION = "1.0.0"

BODY_RADIUS = 1.0
BODY_Y_MIN, BODY_Y_MAX = 0.0, 3.2
THETA_MIN, THETA_MAX = -2.2, 2.2
OUTLINE_U_MIN, OUTLINE_U_MAX = THETA_MIN * BODY_RADIUS, THETA_MAX * BODY_RADIUS
OUTLINE_V_MIN, OUTLINE_V_MAX = 0.0, 4.0

CAP_RADIUS = 0.72
CAP_Y_MIN, CAP_Y_MAX = 3.3, 4.0
CAP_U_MIN, CAP_U_MAX = -2.0, 2.0


def _body_point(u: float, v: float) -> Tuple[float, float, float]:
    theta = u / BODY_RADIUS
    return (BODY_RADIUS * math.sin(theta), v, BODY_RADIUS * math.cos(theta))


def _cap_point(u: float, v: float) -> Tuple[float, float, float]:
    theta = u / CAP_RADIUS
    return (CAP_RADIUS * math.sin(theta), v, CAP_RADIUS * math.cos(theta))


def _build_polyline(
    start: Tuple[float, float], tangent: Tuple[float, float], length: float,
    surface: str,
) -> List[Tuple[float, float, float]]:
    steps = max(2, int(math.ceil(length / 0.04)))
    mapper = _body_point if surface == "body" else _cap_point
    points = []
    for k in range(steps + 1):
        t = length * k / steps
        u = start[0] + tangent[0] * t
        v = start[1] + tangent[1] * t
        points.append(mapper(u, v))
    return points


def _polyline_length(points: List[Tuple[float, float, float]]) -> float:
    total = 0.0
    for a, b in zip(points, points[1:]):
        total += math.sqrt(sum((b[i] - a[i]) ** 2 for i in range(3)))
    return total


def _chamfered_rect(u0: float, v0: float, u1: float, v1: float, c: float) -> List[Tuple[float, float]]:
    return [
        (u0 + c, v0), (u1 - c, v0), (u1, v0 + c), (u1, v1 - c),
        (u1 - c, v1), (u0 + c, v1), (u0, v1 - c), (u0, v0 + c),
    ]


def _sample_start(rng: np.random.Generator, region: Tuple[float, float, float, float],
                  length: float, tangent: Tuple[float, float]) -> Tuple[float, float]:
    u0, v0, u1, v1 = region
    margin_u = min(abs(tangent[0]) * length * 0.5 + 0.01, (u1 - u0) * 0.45)
    margin_v = min(abs(tangent[1]) * length * 0.5 + 0.01, (v1 - v0) * 0.45)
    u = rng.uniform(u0 + margin_u, u1 - margin_u)
    v = rng.uniform(v0 + margin_v, v1 - margin_v)
    return (float(u), float(v))


def generate_project(seed: int = SEED) -> PackagingProject:
    rng = np.random.default_rng(seed)

    body = Surface(
        name="body", material="frosted_glass", radius=BODY_RADIUS,
        y_min=BODY_Y_MIN, y_max=BODY_Y_MAX,
        theta_min=THETA_MIN, theta_max=THETA_MAX,
    )
    cap = Surface(
        name="cap", material="cap_matte", radius=CAP_RADIUS,
        y_min=CAP_Y_MIN, y_max=CAP_Y_MAX,
        theta_min=CAP_U_MIN / CAP_RADIUS, theta_max=CAP_U_MAX / CAP_RADIUS,
    )

    die_lines = [
        DieLine("body-blank", "frosted_glass",
                [(OUTLINE_U_MIN, 0.0), (OUTLINE_U_MAX, 0.0),
                 (OUTLINE_U_MAX, BODY_Y_MAX), (OUTLINE_U_MIN, BODY_Y_MAX)]),
        DieLine("label-panel", "label",
                _chamfered_rect(-1.6, 0.9, 1.6, 2.4, 0.12)),
        DieLine("cap-strip", "cap_matte",
                [(CAP_U_MIN, CAP_Y_MIN), (CAP_U_MAX, CAP_Y_MIN),
                 (CAP_U_MAX, CAP_Y_MAX), (CAP_U_MIN, CAP_Y_MAX)]),
    ]
    labels = [
        LabelUV("front-label", (-1.6, 0.9, 1.6, 2.4), (-1.6, 1.6), (0.9, 2.4)),
    ]
    lights = [
        Light("key", (0.45, 0.8, 0.9), 1.0, 5200.0, 0.55),
        Light("fill", (-0.8, 0.35, 0.55), 0.45, 6100.0, 0.75),
        Light("rim", (0.15, 0.25, -1.0), 0.85, 6500.0, 0.35),
    ]
    camera = Camera(
        name="hero",
        position=(0.0, 2.0, 6.2),
        target=(0.0, 1.9, 0.0),
        up_hint=(0.0, 1.0, 0.0),
        focal_px=560.0,
        width_px=320,
        height_px=420,
    )

    label_region = (-1.5, 1.0, 1.5, 2.3)
    glass_region = (-2.05, 0.15, 2.05, 3.05)
    cap_region = (-1.85, 3.35, 1.85, 3.95)

    curves: List[Curve] = []

    def add_curve(kind: str, material: str, surface: str, region,
                  length_range: Tuple[float, float], radius_range: Tuple[float, float],
                  opacity_range: Tuple[float, float], prefix: str, index: int,
                  rim_guard: bool = False) -> int:
        angle = rng.uniform(0.0, 2.0 * math.pi)
        tangent = (math.cos(angle), math.sin(angle))
        length = float(rng.uniform(*length_range))
        start = _sample_start(rng, region, length, tangent)
        points = _build_polyline(start, tangent, length, surface)
        curve = Curve(
            curve_id=f"{prefix}-{index:04d}",
            kind=kind,
            material=material,
            start_2d=start,
            tangent_2d=tangent,
            declared_length=_polyline_length(points),
            radius_px=float(rng.uniform(*radius_range)),
            opacity=float(rng.uniform(*opacity_range)),
            surface=surface,
            points_3d=points,
            rim_guard=rim_guard,
        )
        curves.append(curve)
        return index + 1

    f_idx, g_idx, r_idx = 1, 1, 1
    for _ in range(64):
        f_idx = add_curve("fiber", "label", "body", label_region,
                          (0.18, 0.55), (0.4, 0.9), (0.55, 0.95), "FB", f_idx)
    for _ in range(46):
        f_idx = add_curve("fiber", "frosted_glass", "body", glass_region,
                          (0.15, 0.5), (0.35, 0.85), (0.35, 0.8), "FB", f_idx)
    for _ in range(22):
        f_idx = add_curve("fiber", "cap_matte", "cap", cap_region,
                          (0.1, 0.32), (0.35, 0.8), (0.5, 0.9), "FB", f_idx)
    for _ in range(150):
        g_idx = add_curve("grain", "frosted_glass", "body", glass_region,
                          (0.015, 0.05), (0.2, 0.5), (0.25, 0.7), "GR", g_idx)
    for _ in range(70):
        g_idx = add_curve("grain", "cap_matte", "cap", cap_region,
                          (0.012, 0.04), (0.2, 0.45), (0.3, 0.75), "GR", g_idx)
    for _ in range(40):
        g_idx = add_curve("grain", "label", "body", label_region,
                          (0.012, 0.04), (0.2, 0.45), (0.3, 0.8), "GR", g_idx)

    # Rim guard chains: continuous vertical fibres on both silhouette edges.
    # The left chain has one missing link, which the contour diagnostic finds.
    rim_u = -math.pi / 2 * BODY_RADIUS
    positions = [0.35 + k * 0.11 for k in range(25)]
    for k, v in enumerate(positions):
        if k == 10:
            continue
        start = (rim_u, v)
        tangent = (0.0, 1.0)
        length = 0.12
        points = _build_polyline(start, tangent, length, "body")
        curves.append(Curve(
            curve_id=f"RM-{r_idx:04d}", kind="fiber", material="frosted_glass",
            start_2d=start, tangent_2d=tangent,
            declared_length=_polyline_length(points),
            radius_px=0.55, opacity=0.9, surface="body",
            points_3d=points, rim_guard=True,
        ))
        r_idx += 1
    rim_u_right = math.pi / 2 * BODY_RADIUS
    for k, v in enumerate(positions):
        start = (rim_u_right, v)
        tangent = (0.0, 1.0)
        length = 0.12
        points = _build_polyline(start, tangent, length, "body")
        curves.append(Curve(
            curve_id=f"RM-{r_idx:04d}", kind="fiber", material="frosted_glass",
            start_2d=start, tangent_2d=tangent,
            declared_length=_polyline_length(points),
            radius_px=0.55, opacity=0.9, surface="body",
            points_3d=points, rim_guard=True,
        ))
        r_idx += 1

    # Deterministically inject inconsistent curves so validation has output.
    tamper_targets = [4, 33, 71, 108, 150, 210, 270, 320]
    tamper_codes = ["E_START_OUTSIDE", "E_TANGENT_NORM", "E_LENGTH_MISMATCH",
                    "E_POINT_OUTSIDE", "E_START_OUTSIDE", "E_TANGENT_NORM",
                    "E_LENGTH_MISMATCH", "E_POINT_OUTSIDE"]
    for target, code in zip(tamper_targets, tamper_codes):
        if target >= len(curves):
            continue
        curve = curves[target]
        if code == "E_START_OUTSIDE":
            curve.start_2d = (OUTLINE_U_MAX + 0.35, curve.start_2d[1])
        elif code == "E_TANGENT_NORM":
            curve.tangent_2d = (curve.tangent_2d[0] * 1.7, curve.tangent_2d[1])
        elif code == "E_LENGTH_MISMATCH":
            curve.declared_length *= 1.6
        else:
            mid = len(curve.points_3d) // 2
            x, y, z = curve.points_3d[mid]
            curve.points_3d[mid] = (x + (OUTLINE_U_MAX + 0.4), y, z)

    project = PackagingProject(
        project_id=PROJECT_ID,
        seed=seed,
        version=ENGINE_VERSION,
        outline_bounds=(OUTLINE_U_MIN, OUTLINE_V_MIN, OUTLINE_U_MAX, OUTLINE_V_MAX),
        body=body,
        cap=cap,
        die_lines=die_lines,
        labels=labels,
        lights=lights,
        camera=camera,
        curves=curves,
        material_of_surface={"body": "frosted_glass", "cap": "cap_matte"},
    )
    return project


def write_project(project: PackagingProject, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(project.to_dict(), handle, ensure_ascii=False, indent=2)


def read_project(path: str) -> PackagingProject:
    with open(path, encoding="utf-8") as handle:
        return PackagingProject.from_dict(json.load(handle))
