from __future__ import annotations

import math

import numpy as np


def normalize_quat(q: np.ndarray) -> np.ndarray:
    arr = np.asarray(q, dtype=np.float64)
    norm = float(np.linalg.norm(arr))
    if norm == 0.0 or not math.isfinite(norm):
        # Validation normally rejects this; keep evaluation deterministic.
        return np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float64)
    return arr / norm


def quat_slerp(q0: np.ndarray, q1: np.ndarray, t: float) -> np.ndarray:
    a = normalize_quat(q0)
    b = normalize_quat(q1)
    dot = float(np.dot(a, b))
    if dot < 0.0:
        b = -b
        dot = -dot
    if dot > 0.9995:
        result = a + (b - a) * t
        return normalize_quat(result)
    theta_0 = math.acos(max(-1.0, min(1.0, dot)))
    sin_theta_0 = math.sin(theta_0)
    theta = theta_0 * t
    sin_theta = math.sin(theta)
    s0 = math.cos(theta) - dot * sin_theta / sin_theta_0
    s1 = sin_theta / sin_theta_0
    return normalize_quat(a * s0 + b * s1)


def quat_to_matrix(q: np.ndarray) -> np.ndarray:
    w, x, y, z = normalize_quat(q)
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return np.array(
        [
            [1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy), 0.0],
            [2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx), 0.0],
            [2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy), 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )


def trs_matrix(
    translation: np.ndarray,
    rotation: np.ndarray,
    scale: np.ndarray | None = None,
) -> np.ndarray:
    matrix = quat_to_matrix(rotation)
    if scale is not None:
        matrix[:3, 0] *= float(scale[0])
        matrix[:3, 1] *= float(scale[1])
        matrix[:3, 2] *= float(scale[2])
    matrix[0, 3] = float(translation[0])
    matrix[1, 3] = float(translation[1])
    matrix[2, 3] = float(translation[2])
    return matrix


def lerp(a: np.ndarray | float, b: np.ndarray | float, t: float):
    return a + (b - a) * t
