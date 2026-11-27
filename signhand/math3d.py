"""Small column-vector 3D math helpers used by the renderer."""

from __future__ import annotations

import math

import numpy as np


def vec3(value, name: str = "vector") -> np.ndarray:
    result = np.asarray(value, dtype=np.float64)
    if result.shape != (3,) or not np.all(np.isfinite(result)):
        raise ValueError(f"{name} must contain three finite numbers")
    return result


def normalize(value: np.ndarray, eps: float = 1e-12) -> np.ndarray:
    value = np.asarray(value, dtype=np.float64)
    norm = np.linalg.norm(value, axis=-1, keepdims=True)
    return value / np.maximum(norm, eps)


def rotation_x(degrees: float) -> np.ndarray:
    angle = math.radians(float(degrees))
    c, s = math.cos(angle), math.sin(angle)
    return np.array(
        [[1.0, 0.0, 0.0, 0.0], [0.0, c, -s, 0.0], [0.0, s, c, 0.0], [0.0, 0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


def rotation_y(degrees: float) -> np.ndarray:
    angle = math.radians(float(degrees))
    c, s = math.cos(angle), math.sin(angle)
    return np.array(
        [[c, 0.0, s, 0.0], [0.0, 1.0, 0.0, 0.0], [-s, 0.0, c, 0.0], [0.0, 0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


def rotation_z(degrees: float) -> np.ndarray:
    angle = math.radians(float(degrees))
    c, s = math.cos(angle), math.sin(angle)
    return np.array(
        [[c, -s, 0.0, 0.0], [s, c, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


def euler_xyz_matrix(degrees) -> np.ndarray:
    """Return Rz @ Ry @ Rx for XYZ Euler angles expressed in degrees."""

    values = vec3(degrees, "Euler angle")
    return rotation_z(values[2]) @ rotation_y(values[1]) @ rotation_x(values[0])


def transform(rotation: np.ndarray | None = None, translation=None) -> np.ndarray:
    result = np.eye(4, dtype=np.float64)
    if rotation is not None:
        result[:3, :3] = np.asarray(rotation, dtype=np.float64)[:3, :3]
    if translation is not None:
        result[:3, 3] = vec3(translation, "translation")
    return result


def inverse_rigid(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float64)
    result = np.eye(4, dtype=np.float64)
    result[:3, :3] = matrix[:3, :3].T
    result[:3, 3] = -result[:3, :3] @ matrix[:3, 3]
    return result


def apply_transform(matrix: np.ndarray, points: np.ndarray, w: float = 1.0) -> np.ndarray:
    points = np.asarray(points, dtype=np.float64)
    matrix = np.asarray(matrix, dtype=np.float64)
    return points @ matrix[:3, :3].T + matrix[:3, 3] * w


def smoothstep(value):
    value = np.clip(value, 0.0, 1.0)
    return value * value * (3.0 - 2.0 * value)

