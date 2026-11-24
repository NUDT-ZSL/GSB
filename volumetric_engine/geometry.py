"""Unified-world ray/box intersection and deterministic medium ownership."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from .model import MediaBox, Vec3


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _length(a: Vec3) -> float:
    return math.sqrt(_dot(a, a))


def _point_at(origin: Vec3, direction: Vec3, t: float) -> Vec3:
    return (
        origin[0] + direction[0] * t,
        origin[1] + direction[1] * t,
        origin[2] + direction[2] * t,
    )


def ray_box_interval(
    origin: Vec3,
    direction: Vec3,
    medium: MediaBox,
    *,
    epsilon: float = 1e-9,
) -> tuple[float, float] | None:
    """Return the (enter, exit) parameter interval of an AABB, or ``None``."""

    t_enter = -math.inf
    t_exit = math.inf
    lo = medium.minimum
    hi = medium.maximum
    for axis in range(3):
        origin_axis = origin[axis]
        direction_axis = direction[axis]
        if abs(direction_axis) <= epsilon:
            if origin_axis < lo[axis] - epsilon or origin_axis > hi[axis] + epsilon:
                return None
            continue
        t0 = (lo[axis] - origin_axis) / direction_axis
        t1 = (hi[axis] - origin_axis) / direction_axis
        if t0 > t1:
            t0, t1 = t1, t0
        t_enter = max(t_enter, t0)
        t_exit = min(t_exit, t1)
        if t_enter > t_exit + epsilon:
            return None
    return t_enter, t_exit


@dataclass(frozen=True)
class OwnedInterval:
    start: float
    end: float
    medium: MediaBox

    @property
    def length(self) -> float:
        return max(0.0, self.end - self.start)


def _unique_sorted(values: Iterable[float], epsilon: float) -> list[float]:
    result: list[float] = []
    for value in sorted(values):
        if not result or abs(value - result[-1]) > epsilon:
            result.append(value)
        else:
            # Preserve the later boundary when two boxes share an edge.
            result[-1] = max(result[-1], value)
    return result


def build_interval_chain(
    origin: Vec3,
    direction: Vec3,
    media: Sequence[MediaBox],
    *,
    t_min: float = 0.0,
    t_max: float = math.inf,
    epsilon: float = 1e-9,
) -> list[OwnedInterval]:
    """Build the ordered, non-overlapping medium chain.

    All box boundaries become event points.  For every elementary interval the
    owning box is selected from the interval midpoint (never from an endpoint),
    which makes boundary ownership stable under tiling and tiny camera moves.
    Higher IOR wins overlaps; declaration order breaks equal-IOR ties.
    """

    if _length(direction) == 0.0:
        return []
    events: list[float] = []
    hits: list[tuple[float, float, MediaBox]] = []
    for medium in media:
        interval = ray_box_interval(origin, direction, medium, epsilon=epsilon)
        if interval is None:
            continue
        enter = max(interval[0], t_min)
        leave = min(interval[1], t_max)
        if leave < enter - epsilon:
            continue
        enter = max(enter, t_min)
        leave = min(leave, t_max)
        if leave < enter:
            continue
        hits.append((enter, leave, medium))
        if math.isfinite(enter):
            events.append(enter)
        if math.isfinite(leave):
            events.append(leave)
    if not hits:
        return []
    finite_min = t_min if math.isfinite(t_min) else min(event for event in events)
    finite_max = t_max if math.isfinite(t_max) else max(event for event in events)
    events.extend([finite_min, finite_max])
    points = _unique_sorted(events, epsilon)

    chain: list[OwnedInterval] = []
    for start, end in zip(points, points[1:]):
        if end <= start + epsilon:
            continue
        midpoint_t = (start + end) * 0.5
        point = _point_at(origin, direction, midpoint_t)
        candidates = [medium for medium in media if medium.contains(point, epsilon=epsilon)]
        if not candidates:
            continue
        owner = max(candidates, key=lambda item: item.priority_key)
        if chain and chain[-1].medium.id == owner.id and abs(chain[-1].end - start) <= epsilon:
            previous = chain[-1]
            chain[-1] = OwnedInterval(previous.start, end, previous.medium)
        else:
            chain.append(OwnedInterval(start, end, owner))
    return chain


def segment_optical_depth(
    origin: Vec3,
    direction: Vec3,
    media: Sequence[MediaBox],
    distance: float,
    *,
    epsilon: float = 1e-9,
) -> float:
    """Owner-resolved scalar optical depth from ``origin`` to ``distance``."""

    if distance <= 0.0 or _length(direction) == 0.0:
        return 0.0
    # A sweep over active boxes avoids materializing a chain for every shadow
    # sample.  Between two events the active set is constant, so the owner is
    # still the priority winner of the open interval's interior.
    events: list[tuple[float, int, MediaBox]] = []
    for medium in media:
        hit = ray_box_interval(origin, direction, medium, epsilon=epsilon)
        if hit is None:
            continue
        enter = max(0.0, hit[0])
        leave = min(distance, hit[1])
        if leave <= enter + epsilon:
            continue
        events.append((enter, 1, medium))
        events.append((leave, -1, medium))
    if not events:
        return 0.0
    events.sort(key=lambda item: (item[0], item[1]))
    active: dict[str, MediaBox] = {}
    total = 0.0
    cursor = events[0][0]
    index = 0
    while index < len(events):
        event_t = events[index][0]
        if event_t > cursor + epsilon and active:
            owner = max(active.values(), key=lambda item: item.priority_key)
            total += (event_t - cursor) * owner.extinction_scalar
        group_end = index
        while group_end < len(events) and abs(events[group_end][0] - event_t) <= epsilon:
            group_end += 1
        for event_index in range(index, group_end):
            _, kind, medium = events[event_index]
            if kind < 0:
                active.pop(medium.id, None)
        for event_index in range(index, group_end):
            _, kind, medium = events[event_index]
            if kind > 0:
                active[medium.id] = medium
        index = group_end
        cursor = event_t
    return total


def light_transmittance(
    point: Vec3,
    light_position: Vec3,
    media: Sequence[MediaBox],
    *,
    epsilon: float = 1e-9,
) -> tuple[float, float, Vec3]:
    """Return (visibility, distance, unit_direction) for a shadow ray."""

    dx = light_position[0] - point[0]
    dy = light_position[1] - point[1]
    dz = light_position[2] - point[2]
    distance = math.sqrt(dx * dx + dy * dy + dz * dz)
    if distance <= epsilon:
        return 1.0, 0.0, (0.0, 0.0, 1.0)
    direction = (dx / distance, dy / distance, dz / distance)
    tau = segment_optical_depth(point, direction, media, distance, epsilon=epsilon)
    return math.exp(-tau), distance, direction

