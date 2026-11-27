from __future__ import annotations

import copy
import math
import random
from typing import Any


def _rounded(value: float) -> float:
    return round(value, 6)


def _forward_to_screen(position: tuple[float, float], screen_center: tuple[float, float]) -> list[float]:
    dx = screen_center[0] - position[0]
    dy = screen_center[1] - position[1]
    length = math.hypot(dx, dy)
    return [_rounded(dx / length), _rounded(dy / length), 0.0]


def build_project(seed: int = 20261128, scenario: str = "base") -> dict[str, Any]:
    """Build the deterministic living-room sample.

    The same seed and scenario always produce byte-identical project data.
    The adjusted scenario is intended for incremental-recalculation demos.
    """

    if scenario not in {"base", "adjusted"}:
        raise ValueError("scenario must be 'base' or 'adjusted'")
    rng = random.Random(seed)
    room = {"width_m": 5.8, "depth_m": 6.2, "height_m": 2.8}
    screen_center = (2.9, 5.95)
    pose_positions = {
        "seat.sofa.left": (1.35, 1.55),
        "seat.sofa.center": (2.9, 1.45),
        "seat.sofa.right": (4.45, 1.55),
        "seat.chair": (5.02, 3.15),
    }
    pose_furniture = {
        "seat.sofa.left": "sofa",
        "seat.sofa.center": "sofa",
        "seat.sofa.right": "sofa",
        "seat.chair": "armchair",
    }
    poses = []
    for pose_id in sorted(pose_positions):
        x_coord, y_coord = pose_positions[pose_id]
        poses.append(
            {
                "id": pose_id,
                "position_m": [
                    _rounded(x_coord),
                    _rounded(y_coord),
                    1.12,
                ],
                "forward": _forward_to_screen(
                    (x_coord, y_coord), screen_center
                ),
                "up": [0.0, 0.0, 1.0],
                "furniture_id": pose_furniture[pose_id],
            }
        )

    lamp_x = 4.38
    lamp_y = 4.82
    if scenario == "adjusted":
        lamp_x -= 0.32
        lamp_y -= 0.18
        pose_positions["seat.chair"] = (
            pose_positions["seat.chair"][0] - 0.12,
            pose_positions["seat.chair"][1] + 0.05,
        )
        for pose in poses:
            if pose["id"] == "seat.chair":
                x_coord, y_coord = pose_positions["seat.chair"]
                pose["position_m"] = [
                    _rounded(x_coord),
                    _rounded(y_coord),
                    1.12,
                ]
                pose["forward"] = _forward_to_screen(
                    (x_coord, y_coord), screen_center
                )

    acoustic_anchor = [
        ("A01", 1.35, 1.55, 0.64, "seat.sofa.left"),
        ("A02", 2.9, 1.45, 0.64, "seat.sofa.center"),
        ("A03", 4.45, 1.55, 0.64, "seat.sofa.right"),
        ("A04", 5.02, 3.15, 0.64, "seat.chair"),
        ("A05", 2.9, 4.45, 0.55, None),
        ("A06", 0.55, 3.35, 0.55, None),
        ("A07", 4.85, 5.15, 0.48, None),
    ]
    acoustic_samples = []
    for sample_id, x_coord, y_coord, radius, pose_id in acoustic_anchor:
        jitter_x = rng.uniform(-0.015, 0.015)
        jitter_y = rng.uniform(-0.015, 0.015)
        acoustic_samples.append(
            {
                "id": sample_id,
                "position_m": [
                    _rounded(x_coord + jitter_x),
                    _rounded(y_coord + jitter_y),
                ],
                "coverage_radius_m": radius,
                **(
                    {"viewing_pose_id": pose_id}
                    if pose_id is not None
                    else {}
                ),
            }
        )

    coffee_min_x = 2.05
    coffee_max_x = 3.75
    coffee_min_y = 2.85
    coffee_max_y = 3.75
    if scenario == "adjusted":
        coffee_min_x += 0.18
        coffee_max_x += 0.18
        coffee_min_y += 0.12
        coffee_max_y += 0.12

    project = {
        "schema_version": 1,
        "project_id": "living-room-stereo-demo",
        "seed": seed,
        "generator": {
            "name": "livingroom-stereo-generator",
            "scenario": scenario,
            "version": 1,
        },
        "room": room,
        "screen": {
            "id": "screen.main",
            "center_m": [2.9, 5.95, 1.3],
            "width_m": 2.9,
            "height_m": 1.5,
            "resolution": {"width_px": 1600, "height_px": 900},
            "blocks": {"cols": 8, "rows": 5},
            "samples_per_block": {"cols": 3, "rows": 3},
            "right": [1.0, 0.0, 0.0],
            "up": [0.0, 0.0, 1.0],
            "normal": [0.0, -1.0, 0.0],
            "design_viewing_distance_m": 4.4,
            "zero_parallax_distance_m": 4.15,
        },
        "stereo": {"interocular_distance_m": 0.064},
        "assessment": {
            "comfort_parallax_px": {"min": -16.0, "max": 10.0},
            "depth_distortion_threshold": 0.1,
            "score_weights": {
                "overflow_ratio": 0.65,
                "overflow_magnitude": 0.35,
            },
            "min_comfortable_block_ratio": 0.3,
            "viewing_zone_radius_m": 1.35,
        },
        "viewing_poses": poses,
        "furniture": [
            {
                "id": "sofa",
                "kind": "sofa",
                "bounds_m": {
                    "min": [0.72, 0.72, 0.15],
                    "max": [5.08, 1.88, 0.95],
                },
            },
            {
                "id": "armchair",
                "kind": "armchair",
                "bounds_m": {
                    "min": [4.52, 2.62, 0.0],
                    "max": [5.5, 3.68, 1.0],
                },
            },
            {
                "id": "coffee_table",
                "kind": "coffee_table",
                "bounds_m": {
                    "min": [
                        _rounded(coffee_min_x),
                        _rounded(coffee_min_y),
                        0.0,
                    ],
                    "max": [
                        _rounded(coffee_max_x),
                        _rounded(coffee_max_y),
                        0.44,
                    ],
                },
            },
            {
                "id": "tv_cabinet",
                "kind": "tv_cabinet",
                "bounds_m": {
                    "min": [1.62, 5.24, 0.0],
                    "max": [4.18, 5.86, 0.62],
                },
            },
            {
                "id": "floor_lamp",
                "kind": "floor_lamp",
                "bounds_m": {
                    "min": [
                        _rounded(lamp_x),
                        _rounded(lamp_y),
                        0.0,
                    ],
                    "max": [
                        _rounded(lamp_x + 0.3),
                        _rounded(lamp_y + 0.3),
                        1.85,
                    ],
                },
            },
            {
                "id": "speaker_left",
                "kind": "speaker",
                "bounds_m": {
                    "min": [1.12, 4.86, 0.0],
                    "max": [1.48, 5.22, 1.22],
                },
            },
        ],
        "relationships": [
            {
                "id": "sofa_to_tv_cabinet",
                "from": "sofa",
                "to": "tv_cabinet",
            },
            {
                "id": "coffee_table_to_screen",
                "from": "coffee_table",
                "to": "screen:screen.main",
            },
            {
                "id": "armchair_to_tv_cabinet",
                "from": "armchair",
                "to": "tv_cabinet",
            },
        ],
        "acoustic_samples": acoustic_samples,
    }
    return project


def adjusted_project_from(base: dict[str, Any], seed: int | None = None) -> dict[str, Any]:
    """Return a deterministic modified project for incremental demos."""

    del base  # retained as an explicit API boundary for future generators
    actual_seed = 20261128 if seed is None else seed
    return build_project(seed=actual_seed, scenario="adjusted")


def clone_project(project: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(project)
