from __future__ import annotations

import math

from .model import Furniture, Room, Vec3

EPSILON = 1e-12


def add(left: Vec3, right: Vec3) -> Vec3:
    return (
        left[0] + right[0],
        left[1] + right[1],
        left[2] + right[2],
    )


def subtract(left: Vec3, right: Vec3) -> Vec3:
    return (
        left[0] - right[0],
        left[1] - right[1],
        left[2] - right[2],
    )


def scale(vector: Vec3, factor: float) -> Vec3:
    return (vector[0] * factor, vector[1] * factor, vector[2] * factor)


def dot(left: Vec3, right: Vec3) -> float:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def cross(left: Vec3, right: Vec3) -> Vec3:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def norm(vector: Vec3) -> float:
    return math.sqrt(dot(vector, vector))


def normalize(vector: Vec3) -> Vec3:
    length = norm(vector)
    if length <= EPSILON:
        raise ValueError("cannot normalise a zero-length vector")
    return scale(vector, 1.0 / length)


def distance(left: Vec3, right: Vec3) -> float:
    return norm(subtract(left, right))


def interpolate(origin: Vec3, target: Vec3, factor: float) -> Vec3:
    return add(origin, scale(subtract(target, origin), factor))


def ray_aabb_interval(
    origin: Vec3,
    direction: Vec3,
    minimum: Vec3,
    maximum: Vec3,
) -> tuple[float, float] | None:
    """Return the ray parameter interval inside an axis-aligned box."""

    enter = -math.inf
    exit_ = math.inf
    for axis in range(3):
        component = direction[axis]
        if abs(component) <= EPSILON:
            if origin[axis] < minimum[axis] or origin[axis] > maximum[axis]:
                return None
            continue
        first = (minimum[axis] - origin[axis]) / component
        second = (maximum[axis] - origin[axis]) / component
        if first > second:
            first, second = second, first
        enter = max(enter, first)
        exit_ = min(exit_, second)
        if exit_ < enter:
            return None
    return enter, exit_


def ray_room_exit(origin: Vec3, direction: Vec3, room: Room) -> float:
    interval = ray_aabb_interval(
        origin, direction, room.minimum, room.maximum
    )
    if interval is None or interval[1] <= 0.0:
        raise ValueError("viewing ray does not leave the room")
    return interval[1]


def ray_furniture_interval(
    origin: Vec3,
    direction: Vec3,
    furniture: Furniture,
) -> tuple[float, float] | None:
    return ray_aabb_interval(
        origin, direction, furniture.minimum, furniture.maximum
    )
