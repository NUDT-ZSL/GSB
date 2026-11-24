from __future__ import annotations

import math
from typing import Sequence

Vec3 = tuple[float, float, float]
Quat = tuple[float, float, float, float]
Transform = tuple[Vec3, Quat, Vec3]

IDENTITY_QUAT: Quat = (0.0, 0.0, 0.0, 1.0)
ZERO_VEC: Vec3 = (0.0, 0.0, 0.0)
ONE_VEC: Vec3 = (1.0, 1.0, 1.0)


def is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clamp(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def vec3(values: Sequence[float]) -> Vec3:
    return (float(values[0]), float(values[1]), float(values[2]))


def quat(values: Sequence[float]) -> Quat:
    return (float(values[0]), float(values[1]), float(values[2]), float(values[3]))


def lerp(a: float, b: float, alpha: float) -> float:
    return a + (b - a) * alpha


def lerp_vec3(a: Vec3, b: Vec3, alpha: float) -> Vec3:
    return (
        lerp(a[0], b[0], alpha),
        lerp(a[1], b[1], alpha),
        lerp(a[2], b[2], alpha),
    )


def quat_dot(a: Quat, b: Quat) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]


def quat_norm(a: Quat) -> float:
    return math.sqrt(quat_dot(a, a))


def quat_normalize(a: Quat) -> Quat:
    length = quat_norm(a)
    if length == 0.0:
        raise ValueError("zero quaternion")
    return (a[0] / length, a[1] / length, a[2] / length, a[3] / length)


def quat_negate(a: Quat) -> Quat:
    return (-a[0], -a[1], -a[2], -a[3])


def quat_slerp(a: Quat, b: Quat, alpha: float) -> Quat:
    """Shortest-arc slerp with a fixed sign convention."""
    a = quat_normalize(a)
    b = quat_normalize(b)
    dot = quat_dot(a, b)
    if dot < 0.0:
        b = quat_negate(b)
        dot = -dot
    if dot > 0.9995:
        return quat_normalize(
            (
                lerp(a[0], b[0], alpha),
                lerp(a[1], b[1], alpha),
                lerp(a[2], b[2], alpha),
                lerp(a[3], b[3], alpha),
            )
        )
    theta0 = math.acos(clamp(dot, -1.0, 1.0))
    sin_theta0 = math.sin(theta0)
    if sin_theta0 == 0.0:
        return a
    theta = theta0 * alpha
    s0 = math.sin(theta0 - theta) / sin_theta0
    s1 = math.sin(theta) / sin_theta0
    return quat_normalize(
        (
            a[0] * s0 + b[0] * s1,
            a[1] * s0 + b[1] * s1,
            a[2] * s0 + b[2] * s1,
            a[3] * s0 + b[3] * s1,
        )
    )


def quat_mul(a: Quat, b: Quat) -> Quat:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quat_rotate(q: Quat, v: Vec3) -> Vec3:
    q = quat_normalize(q)
    x, y, z, w = q
    vx, vy, vz = v
    # Expanded q * (v, 0) * conjugate(q).
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def compose(parent: Transform, local: Transform) -> Transform:
    parent_t, parent_r, parent_s = parent
    local_t, local_r, local_s = local
    scaled = (local_t[0] * parent_s[0], local_t[1] * parent_s[1], local_t[2] * parent_s[2])
    rotated = quat_rotate(parent_r, scaled)
    world_t = (
        parent_t[0] + rotated[0],
        parent_t[1] + rotated[1],
        parent_t[2] + rotated[2],
    )
    world_r = quat_normalize(quat_mul(parent_r, local_r))
    world_s = (
        parent_s[0] * local_s[0],
        parent_s[1] * local_s[1],
        parent_s[2] * local_s[2],
    )
    return (world_t, world_r, world_s)

