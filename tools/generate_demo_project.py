#!/usr/bin/env python3
"""Generate a deterministic hand project and its key-moment list."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from signhand.math3d import euler_xyz_matrix, normalize, smoothstep, transform  # noqa: E402


FINGERS = {
    "index": {
        "lengths": [0.43, 0.30, 0.25],
        "radii": [0.085, 0.075, 0.065],
        "base": [-0.34, 0.612, 0.02],
        "rest_z": 4.0,
        "color": [0.72, 0.51, 0.40],
    },
    "middle": {
        "lengths": [0.47, 0.34, 0.27],
        "radii": [0.090, 0.080, 0.067],
        "base": [-0.08, 0.735, 0.02],
        "rest_z": 0.0,
        "color": [0.80, 0.59, 0.47],
    },
    "ring": {
        "lengths": [0.42, 0.31, 0.24],
        "radii": [0.082, 0.072, 0.061],
        "base": [0.17, 0.712, 0.02],
        "rest_z": -4.0,
        "color": [0.69, 0.48, 0.38],
    },
    "pinky": {
        "lengths": [0.34, 0.25, 0.21],
        "radii": [0.070, 0.062, 0.052],
        "base": [0.38, 0.572, 0.02],
        "rest_z": -9.0,
        "color": [0.64, 0.44, 0.35],
    },
    "thumb": {
        "lengths": [0.40, 0.32, 0.27],
        "radii": [0.100, 0.085, 0.070],
        "base": [-0.565, -0.345, 0.085],
        "rest_z": 42.0,
        "color": [0.76, 0.53, 0.41],
    },
}


def _ring(center, basis_u, basis_v, radius, segments):
    angles = np.linspace(0.0, 2.0 * np.pi, segments, endpoint=False)
    return (
        center[None, :]
        + radius * np.cos(angles)[:, None] * basis_u[None, :]
        + radius * np.sin(angles)[:, None] * basis_v[None, :]
    )


def _orient_outward(vertices: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    first = vertices[triangles[:, 0]]
    second = vertices[triangles[:, 1]]
    third = vertices[triangles[:, 2]]
    signed = np.einsum("ij,ij->i", first, np.cross(second, third)).sum()
    if signed < 0.0:
        triangles = triangles[:, [0, 2, 1]]
    return triangles


def capsule_mesh(a, b, radius: float, segments: int = 12, cap_steps: int = 4):
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    axis_vector = b - a
    length = float(np.linalg.norm(axis_vector))
    axis = axis_vector / length
    helper = np.array([0.0, 0.0, 1.0])
    if abs(float(np.dot(axis, helper))) > 0.92:
        helper = np.array([1.0, 0.0, 0.0])
    basis_u = normalize(np.cross(axis, helper))
    basis_v = np.cross(axis, basis_u)

    vertices = [a - axis * radius]
    ring_starts = []
    for step in range(1, cap_steps + 1):
        theta = (math.pi * 0.5) * step / cap_steps
        center = a + axis * (-radius * math.cos(theta))
        ring_starts.append(len(vertices))
        vertices.extend(_ring(center, basis_u, basis_v, radius * math.sin(theta), segments))
    for step in range(cap_steps):
        alpha = (math.pi * 0.5) * step / cap_steps
        center = b + axis * (radius * math.sin(alpha))
        ring_starts.append(len(vertices))
        vertices.extend(_ring(center, basis_u, basis_v, radius * math.cos(alpha), segments))
    top_pole_index = len(vertices)
    vertices.append(b + axis * radius)
    vertices_array = np.asarray(vertices, dtype=np.float64)

    triangles: list[list[int]] = []
    first_ring = ring_starts[0]
    for segment in range(segments):
        triangles.append(
            [0, first_ring + segment, first_ring + (segment + 1) % segments]
        )
    for lower, upper in zip(ring_starts[:-1], ring_starts[1:]):
        for segment in range(segments):
            following = (segment + 1) % segments
            lower_0, lower_1 = lower + segment, lower + following
            upper_0, upper_1 = upper + segment, upper + following
            triangles.append([lower_0, upper_0, lower_1])
            triangles.append([lower_1, upper_0, upper_1])
    last_ring = ring_starts[-1]
    for segment in range(segments):
        triangles.append(
            [last_ring + segment, top_pole_index, last_ring + (segment + 1) % segments]
        )
    triangle_array = _orient_outward(
        vertices_array, np.asarray(triangles, dtype=np.int64)
    )
    axis_length = max(length, 1e-12)
    axis_parameter = np.clip(
        ((vertices_array - a) @ axis_vector) / (axis_length * axis_length), 0.0, 1.0
    )
    return vertices_array, triangle_array, axis_parameter


def ellipsoid_mesh(center, radii, segments: int = 16, latitude_steps: int = 8):
    center = np.asarray(center, dtype=np.float64)
    radii = np.asarray(radii, dtype=np.float64)
    vertices = [center + np.array([0.0, 0.0, -radii[2]])]
    ring_starts = []
    for latitude_index in range(1, latitude_steps):
        latitude = -math.pi * 0.5 + math.pi * latitude_index / latitude_steps
        ring_starts.append(len(vertices))
        cos_latitude = math.cos(latitude)
        sin_latitude = math.sin(latitude)
        angles = np.linspace(0.0, 2.0 * np.pi, segments, endpoint=False)
        ring = center + np.stack(
            [
                radii[0] * cos_latitude * np.cos(angles),
                radii[1] * cos_latitude * np.sin(angles),
                np.full(segments, radii[2] * sin_latitude),
            ],
            axis=1,
        )
        vertices.extend(ring)
    top_index = len(vertices)
    vertices.append(center + np.array([0.0, 0.0, radii[2]]))
    vertices_array = np.asarray(vertices, dtype=np.float64)
    triangles: list[list[int]] = []
    first_ring = ring_starts[0]
    for segment in range(segments):
        triangles.append(
            [0, first_ring + (segment + 1) % segments, first_ring + segment]
        )
    for lower, upper in zip(ring_starts[:-1], ring_starts[1:]):
        for segment in range(segments):
            following = (segment + 1) % segments
            lower_0, lower_1 = lower + segment, lower + following
            upper_0, upper_1 = upper + segment, upper + following
            triangles.append([lower_0, lower_1, upper_0])
            triangles.append([lower_1, upper_1, upper_0])
    last_ring = ring_starts[-1]
    for segment in range(segments):
        triangles.append(
            [last_ring + segment, last_ring + (segment + 1) % segments, top_index]
        )
    triangle_array = _orient_outward(
        vertices_array, np.asarray(triangles, dtype=np.int64)
    )
    return vertices_array, triangle_array, np.zeros(vertices_array.shape[0])


def rest_world_matrices(joints: list[dict]) -> dict[str, np.ndarray]:
    lookup = {joint["name"]: joint for joint in joints}
    result: dict[str, np.ndarray] = {}

    def evaluate(name: str) -> np.ndarray:
        if name in result:
            return result[name]
        joint = lookup[name]
        local = transform(
            rotation=euler_xyz_matrix(joint["rest_rotation_euler_deg"]),
            translation=joint["rest_translation"],
        )
        parent = joint["parent"]
        result[name] = local if parent is None else evaluate(parent) @ local
        return result[name]

    for joint in joints:
        evaluate(joint["name"])
    return result


def build_skeleton() -> list[dict]:
    joints = [
        {
            "name": "palm",
            "parent": None,
            "rest_translation": [0.0, 0.0, 0.0],
            "rest_rotation_euler_deg": [0.0, 0.0, 0.0],
        }
    ]
    for finger, values in FINGERS.items():
        for segment in range(3):
            name = f"{finger}_{segment + 1:02d}"
            parent = "palm" if segment == 0 else f"{finger}_{segment:02d}"
            translation = (
                values["base"]
                if segment == 0
                else [0.0, values["lengths"][segment - 1], 0.0]
            )
            rest_z = values["rest_z"] if segment == 0 else (
                -10.0 if finger == "thumb" else 0.0
            )
            joints.append(
                {
                    "name": name,
                    "parent": parent,
                    "rest_translation": translation,
                    "rest_rotation_euler_deg": [0.0, 0.0, rest_z],
                }
            )
    return joints


def build_primitives() -> list[dict]:
    primitives = [
        {
            "id": "palm_shell",
            "type": "ellipsoid",
            "bone": "palm",
            "center": [0.0, 0.0, 0.0],
            "radii": [0.58, 0.72, 0.25],
            "color": [0.77, 0.57, 0.45],
        }
    ]
    for finger, values in FINGERS.items():
        for segment, (length, radius) in enumerate(
            zip(values["lengths"], values["radii"])
        ):
            name = f"{finger}_{segment + 1:02d}"
            if segment == 0:
                start = radius
            else:
                parent_radius = values["radii"][segment - 1]
                start = 0.65 * parent_radius + radius + 0.002
            end = length - 0.35 * radius
            primitives.append(
                {
                    "id": f"{name}_surface",
                    "type": "capsule",
                    "bone": name,
                    "a": [0.0, start, 0.0],
                    "b": [0.0, end, 0.0],
                    "radius": radius,
                    "color": values["color"],
                }
            )
    return primitives


def build_mesh(joints: list[dict], primitives: list[dict]):
    world = rest_world_matrices(joints)
    parent_of = {joint["name"]: joint["parent"] for joint in joints}
    vertices: list[np.ndarray] = []
    triangles: list[list[int]] = []
    vertex_weights: list[dict[str, float]] = []
    vertex_parts: list[str] = []
    triangle_parts: list[str] = []

    for primitive in primitives:
        if primitive["type"] == "capsule":
            local_vertices, local_triangles, parameter = capsule_mesh(
                primitive["a"], primitive["b"], float(primitive["radius"])
            )
        elif primitive["type"] == "ellipsoid":
            local_vertices, local_triangles, parameter = ellipsoid_mesh(
                primitive["center"], primitive["radii"]
            )
        else:
            raise ValueError(f"mesh generation does not support {primitive['type']}")
        bone = primitive["bone"]
        matrix = world[bone]
        transformed = (
            np.concatenate(
                [local_vertices, np.ones((local_vertices.shape[0], 1))], axis=1
            )
            @ matrix.T
        )[:, :3]
        offset = len(vertices)
        vertices.extend(transformed)
        triangles.extend((local_triangles + offset).tolist())
        triangle_parts.extend([bone] * local_triangles.shape[0])
        parent = parent_of[bone]
        for point_parameter in parameter:
            parent_weight = (
                0.0
                if parent is None or primitive["type"] == "ellipsoid"
                else 0.35 * (1.0 - float(smoothstep(point_parameter / 0.45)))
            )
            weights = {bone: 1.0 - parent_weight}
            if parent is not None and parent_weight > 1e-8:
                weights[parent] = parent_weight
            vertex_weights.append(weights)
            vertex_parts.append(bone)

    return {
        "vertices": np.vstack(vertices).tolist(),
        "triangles": triangles,
        "vertex_weights": vertex_weights,
        "vertex_parts": vertex_parts,
        "triangle_parts": triangle_parts,
    }


def _dense_keys(values, interpolation="linear"):
    return {
        "interpolation": interpolation,
        "keys": [[frame, value] for frame, value in enumerate(values)],
    }


def build_animation() -> dict:
    tracks = {}

    def add(name, rotation_keys=None, translation_keys=None, interpolation="cubic"):
        track = {}
        if rotation_keys is not None:
            track["rotation_euler_deg"] = {
                "interpolation": interpolation,
                "keys": rotation_keys,
            }
        if translation_keys is not None:
            track["translation"] = {
                "interpolation": interpolation,
                "keys": translation_keys,
            }
        tracks[name] = track

    add(
        "palm",
        rotation_keys=[
            [0, [0.0, 0.0, 0.0]],
            [3, [4.0, 0.0, 0.0]],
            [5, [9.0, -3.0, 1.0]],
            [7, [28.0, 13.0, 7.0]],
            [8, [62.0, 32.0, 18.0]],
        ],
        translation_keys=[
            [0, [0.0, 0.0, 0.0]],
            [4, [0.015, 0.0, 0.0]],
            [6, [0.035, -0.01, 0.0]],
            [8, [0.06, 0.02, 0.0]],
        ],
    )

    add(
        "index_01",
        [[0, [0, 0, 0]], [2, [0, 0, -5]], [4, [0, 0, -10]], [6, [0, 0, -42]], [7, [0, 0, -22]], [8, [0, 0, -15]]],
    )
    add(
        "index_02",
        [[0, [0, 0, 0]], [2, [-20, 0, 0]], [4, [-31, 0, 0]], [6, [-55, 0, -5]], [7, [-28, 0, 0]], [8, [-20, 0, 0]]],
    )
    add(
        "index_03",
        [[0, [0, 0, 0]], [2, [-12, 0, 0]], [4, [-20, 0, 0]], [6, [-34, 0, 0]], [7, [-16, 0, 0]], [8, [-12, 0, 0]]],
    )
    add(
        "middle_01",
        [[0, [0, 0, 0]], [2, [0, 0, 4]], [4, [0, 0, 10]], [6, [0, 0, 42]], [7, [0, 0, 18]], [8, [0, 0, 12]]],
    )
    add(
        "middle_02",
        [[0, [0, 0, 0]], [2, [-15, 0, 0]], [4, [-26, 0, 0]], [6, [-48, 0, 4]], [7, [-22, 0, 0]], [8, [-15, 0, 0]]],
    )
    add(
        "middle_03",
        [[0, [0, 0, 0]], [2, [-9, 0, 0]], [4, [-16, 0, 0]], [6, [-28, 0, 0]], [7, [-13, 0, 0]], [8, [-8, 0, 0]]],
    )
    add(
        "ring_01",
        [[0, [0, 0, 0]], [2, [0, 0, -2]], [4, [0, 0, -5]], [6, [0, 0, -10]], [8, [0, 0, -8]]],
    )
    add(
        "ring_02",
        [[0, [0, 0, 0]], [2, [-12, 0, 0]], [4, [-22, 0, 0]], [6, [-36, 0, 0]], [8, [-18, 0, 0]]],
    )
    add(
        "ring_03",
        [[0, [0, 0, 0]], [2, [-8, 0, 0]], [4, [-14, 0, 0]], [6, [-22, 0, 0]], [8, [-10, 0, 0]]],
    )
    add(
        "pinky_01",
        [[0, [0, 0, 0]], [2, [0, 0, -2]], [4, [0, 0, -4]], [6, [0, 0, -8]], [8, [0, 0, -6]]],
    )
    add(
        "pinky_02",
        [[0, [0, 0, 0]], [2, [-10, 0, 0]], [4, [-18, 0, 0]], [6, [-28, 0, 0]], [8, [-14, 0, 0]]],
    )
    add(
        "pinky_03",
        [[0, [0, 0, 0]], [2, [-6, 0, 0]], [4, [-11, 0, 0]], [6, [-18, 0, 0]], [8, [-8, 0, 0]]],
    )
    add(
        "thumb_01",
        [[0, [0, 0, 0]], [2, [-8, 0, -20]], [4, [-18, 0, -48]], [6, [-24, 0, -86]], [7, [-30, 0, -112]], [8, [-24, 0, -96]]],
    )
    add(
        "thumb_02",
        [[0, [0, 0, 0]], [2, [-10, 0, 8]], [4, [-18, 0, 18]], [6, [-24, 0, 28]], [7, [-30, 0, 35]], [8, [-24, 0, 30]]],
    )
    add(
        "thumb_03",
        [[0, [0, 0, 0]], [2, [-6, 0, 5]], [4, [-12, 0, 12]], [6, [-18, 0, 18]], [7, [-22, 0, 20]], [8, [-16, 0, 18]]],
    )
    return {"tracks": tracks}


def build_project() -> dict:
    joints = build_skeleton()
    primitives = build_primitives()
    return {
        "schema_version": "1.0",
        "project_id": "public-service-sign-guide",
        "project_version": "guide-v1",
        "fps": 12.0,
        "frame_range": [0, 8],
        "skeleton": {"root": "palm", "joints": joints},
        "animation": build_animation(),
        "sdf_primitives": primitives,
        "mesh": build_mesh(joints, primitives),
        "camera": {
            "mode": "orthographic",
            "position": [0.0, 0.48, 6.0],
            "target": [0.0, 0.48, 0.0],
            "up": [0.0, 1.0, 0.0],
            "near": 0.05,
            "far": 8.0,
            "half_width": 1.45,
            "half_height": 1.45,
        },
        "render": {
            "width": 128,
            "height": 128,
            "max_steps": 256,
            "epsilon": 0.0015,
            "step_safety": 0.8,
            "min_step": 0.000001,
            "max_step": 0.08,
            "gradient_epsilon": 0.0008,
            "smooth_union_radius": 0.08,
            "ambient": 0.24,
            "light_direction": [-0.45, 0.65, 1.0],
            "background_top": [0.055, 0.075, 0.11],
            "background_bottom": [0.012, 0.016, 0.025],
        },
        "collision": {
            "minimum_penetration": 0.00001,
            "surface_samples": 192,
            "grid_resolution": 7,
            "refinement_iterations": 32,
        },
        "deformation_verification": {
            "bones": [
                "index_01",
                "index_02",
                "index_03",
                "middle_01",
                "middle_02",
                "middle_03",
                "ring_01",
                "ring_02",
                "pinky_01",
                "pinky_02",
                "thumb_01",
                "thumb_02",
                "thumb_03",
            ],
            "shrink_volume_ratio": 0.08,
            "expand_volume_ratio": 0.12,
            "surface_distance_tolerance": 0.012,
            "surface_error_quantile": 0.95,
        },
        "metadata": {
            "generator": "tools/generate_demo_project.py",
            "description": "Deterministic public-service sign-language hand exercise.",
            "units": "meters",
            "coordinate_system": "x right, y up, z toward camera",
        },
    }


def build_key_moments() -> dict:
    return {
        "schema_version": "1.0",
        "project_version": "guide-v1",
        "key_moments": [
            {"frame": 0, "label": "neutral_open_hand"},
            {"frame": 2, "label": "index_curl"},
            {"frame": 4, "label": "thumb_index_pinch"},
            {"frame": 6, "label": "tight_finger_contact"},
            {"frame": 7, "label": "thumb_palm_stress"},
            {"frame": 8, "label": "extreme_wrist_rotation"},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        default="examples/generated",
        help="relative output directory",
    )
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    project = build_project()
    moments = build_key_moments()
    (output / "sign_actions.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "key_moments.json").write_text(
        json.dumps(moments, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {output / 'sign_actions.json'}")
    print(f"wrote {output / 'key_moments.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

