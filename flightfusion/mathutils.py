"""Small deterministic quaternion helpers shared by generator and fusion."""

from __future__ import annotations

import math
from typing import Iterable


QUAT = tuple[float, float, float, float]
VEC3 = tuple[float, float, float]


def quat_normalize(q: Iterable[float]) -> QUAT:
    values = tuple(float(item) for item in q)
    norm = math.sqrt(sum(item * item for item in values))
    if norm <= 1e-15:
        return (1.0, 0.0, 0.0, 0.0)
    return tuple(item / norm for item in values)  # type: ignore[return-value]


def quat_conjugate(q: QUAT) -> QUAT:
    return (q[0], -q[1], -q[2], -q[3])


def quat_multiply(a: QUAT, b: QUAT) -> QUAT:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return quat_normalize(
        (
            aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
        )
    )


def quat_from_euler_zyx(yaw: float, pitch: float, roll: float) -> QUAT:
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    return quat_normalize(
        (
            cr * cp * cy + sr * sp * sy,
            sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy,
        )
    )


def quat_to_euler_zyx(q: QUAT) -> VEC3:
    w, x, y, z = quat_normalize(q)
    sinr_cosp = 2 * (w * x + y * z)
    cosr_cosp = 1 - 2 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)
    sinp = 2 * (w * y - z * x)
    sinp = max(-1.0, min(1.0, sinp))
    pitch = math.asin(sinp)
    siny_cosp = 2 * (w * z + x * y)
    cosy_cosp = 1 - 2 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)
    return (roll, pitch, yaw)


def rotate_world_to_body(vector: Iterable[float], q: QUAT) -> VEC3:
    """Rotate a world-frame vector into the body frame for body->world ``q``."""

    vx, vy, vz = (float(item) for item in vector)
    return _rotate((vx, vy, vz), quat_conjugate(q))


def rotate_body_to_world(vector: Iterable[float], q: QUAT) -> VEC3:
    vx, vy, vz = (float(item) for item in vector)
    return _rotate((vx, vy, vz), q)


def _rotate(vector: VEC3, q: QUAT) -> VEC3:
    qv = (q[1], q[2], q[3])
    uv = (
        qv[1] * vector[2] - qv[2] * vector[1],
        qv[2] * vector[0] - qv[0] * vector[2],
        qv[0] * vector[1] - qv[1] * vector[0],
    )
    uuv = (
        qv[1] * uv[2] - qv[2] * uv[1],
        qv[2] * uv[0] - qv[0] * uv[2],
        qv[0] * uv[1] - qv[1] * uv[0],
    )
    return (
        vector[0] + 2.0 * (q[0] * uv[0] + uuv[0]),
        vector[1] + 2.0 * (q[0] * uv[1] + uuv[1]),
        vector[2] + 2.0 * (q[0] * uv[2] + uuv[2]),
    )


def quat_from_rotation_vector(vector: Iterable[float]) -> QUAT:
    vx, vy, vz = (float(item) for item in vector)
    angle = math.sqrt(vx * vx + vy * vy + vz * vz)
    if angle <= 1e-15:
        return (1.0, 0.0, 0.0, 0.0)
    scale = math.sin(angle / 2.0) / angle
    return quat_normalize((math.cos(angle / 2.0), vx * scale, vy * scale, vz * scale))


def quat_angular_distance_deg(a: Iterable[float], b: Iterable[float]) -> float:
    qa = quat_normalize(a)
    qb = quat_normalize(b)
    dot = abs(sum(left * right for left, right in zip(qa, qb)))
    dot = max(-1.0, min(1.0, dot))
    return math.degrees(2.0 * math.acos(dot))


def normalize3(vector: Iterable[float], fallback: VEC3 = (0.0, 0.0, 1.0)) -> VEC3:
    values = tuple(float(item) for item in vector)
    norm = math.sqrt(sum(item * item for item in values))
    if norm <= 1e-15:
        return fallback
    return tuple(item / norm for item in values)  # type: ignore[return-value]


def cross(a: Iterable[float], b: Iterable[float]) -> VEC3:
    ax, ay, az = a
    bx, by, bz = b
    return (ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx)


def dot(a: Iterable[float], b: Iterable[float]) -> float:
    return sum(left * right for left, right in zip(a, b))

