from __future__ import annotations

import math
from dataclasses import dataclass

from .geometry import (
    EPSILON,
    add,
    cross,
    dot,
    normalize,
    ray_aabb_interval,
    ray_room_exit,
    scale,
    subtract,
)
from .model import Project, Screen, Vec3, ViewingPose
from .utils import canonical_float


@dataclass(frozen=True)
class SceneHit:
    point: Vec3
    distance_m: float
    object_id: str
    object_kind: str


@dataclass(frozen=True)
class StereoProjection:
    raw_parallax_px: float
    display_parallax_px: float
    reconstructed_depth_m: float | None
    nominal_depth_m: float
    screen_distance_m: float
    raw_parallax_m: float
    zero_parallax_offset_m: float
    effective_baseline_m: float


@dataclass(frozen=True)
class PoseGeometry:
    pose: ViewingPose
    right: Vec3
    eye_left: Vec3
    eye_right: Vec3
    screen_distance_m: float
    zero_plane_projection_left: Vec3 | None
    zero_plane_projection_right: Vec3 | None
    zero_parallax_offset_m: float | None


def pose_geometry(project: Project, pose: ViewingPose) -> PoseGeometry:
    right = normalize(cross(pose.forward, pose.up))
    half_baseline = project.stereo.interocular_distance_m / 2.0
    eye_left = add(pose.position, scale(right, -half_baseline))
    eye_right = add(pose.position, scale(right, half_baseline))
    screen = project.screen
    screen_distance = dot(
        subtract(pose.position, screen.center), screen.normal
    )
    zero_target = add(
        pose.position,
        scale(pose.forward, screen.zero_parallax_distance_m),
    )
    left_hit = _project_to_screen_plane(eye_left, zero_target, screen)
    right_hit = _project_to_screen_plane(eye_right, zero_target, screen)
    offset: float | None = None
    if left_hit is not None and right_hit is not None:
        offset = dot(subtract(right_hit, left_hit), screen.right)
    return PoseGeometry(
        pose=pose,
        right=right,
        eye_left=eye_left,
        eye_right=eye_right,
        screen_distance_m=screen_distance,
        zero_plane_projection_left=left_hit,
        zero_plane_projection_right=right_hit,
        zero_parallax_offset_m=offset,
    )


def _project_to_screen_plane(
    eye: Vec3,
    target: Vec3,
    screen: Screen,
) -> Vec3 | None:
    numerator = dot(subtract(screen.center, eye), screen.normal)
    denominator = dot(subtract(target, eye), screen.normal)
    if abs(denominator) <= EPSILON:
        return None
    parameter = numerator / denominator
    if not math.isfinite(parameter) or parameter <= 0.0:
        return None
    point = add(eye, scale(subtract(target, eye), parameter))
    return point


def screen_sample_point(
    screen: Screen,
    sample_row: int,
    sample_col: int,
) -> Vec3:
    sample_cols, sample_rows = screen.sample_grid
    if not 0 <= sample_row < sample_rows:
        raise IndexError("sample row outside screen grid")
    if not 0 <= sample_col < sample_cols:
        raise IndexError("sample column outside screen grid")
    horizontal = (sample_col + 0.5) / sample_cols - 0.5
    vertical = (sample_row + 0.5) / sample_rows - 0.5
    return add(
        add(
            screen.center,
            scale(screen.right, horizontal * screen.width_m),
        ),
        scale(screen.up, vertical * screen.height_m),
    )


def cast_scene_hit(
    project: Project,
    pose: ViewingPose,
    screen_point: Vec3,
) -> SceneHit:
    """Find the first furniture or room boundary along the view ray."""

    direction = normalize(subtract(screen_point, pose.position))
    room_parameter = ray_room_exit(pose.position, direction, project.room)
    best_parameter = room_parameter
    best_id = "room"
    best_kind = "room_boundary"

    for item in sorted(project.furniture, key=lambda value: value.id):
        if item.id == pose.furniture_id:
            continue
        interval = ray_aabb_interval(
            pose.position, direction, item.minimum, item.maximum
        )
        if interval is None:
            continue
        entry, exit_ = interval
        if entry <= 0.05 < exit_:
            continue
        parameter = entry
        if parameter <= 0.05:
            continue
        if parameter < best_parameter - 1e-10:
            best_parameter = parameter
            best_id = item.id
            best_kind = item.kind
        elif (
            abs(parameter - best_parameter) <= 1e-10
            and item.id < best_id
        ):
            best_id = item.id
            best_kind = item.kind

    point = add(pose.position, scale(direction, best_parameter))
    return SceneHit(
        point=point,
        distance_m=best_parameter,
        object_id=best_id,
        object_kind=best_kind,
    )


def compute_stereo_projection(
    project: Project,
    pose: ViewingPose,
    scene_hit: SceneHit,
    geometry: PoseGeometry | None = None,
) -> StereoProjection:
    """Compute signed screen parallax and reconstructed depth.

    Sign convention:
      negative -> crossed/convergent disparity (reconstructed in front of
                  the zero-parallax plane)
      positive -> uncrossed/divergent disparity (reconstructed behind the
                  zero-parallax plane)
    """

    pose_geometry_value = geometry or pose_geometry(project, pose)
    screen = project.screen
    if pose_geometry_value.screen_distance_m <= EPSILON:
        raise ValueError("viewing pose is not in front of the screen")
    if pose_geometry_value.zero_parallax_offset_m is None:
        raise ValueError("zero-parallax plane cannot be projected onto screen")

    left_hit = _project_to_screen_plane(
        pose_geometry_value.eye_left, scene_hit.point, screen
    )
    right_hit = _project_to_screen_plane(
        pose_geometry_value.eye_right, scene_hit.point, screen
    )
    if left_hit is None or right_hit is None:
        raise ValueError("scene sample cannot be projected onto screen")

    projected_offset = dot(subtract(right_hit, left_hit), screen.right)
    raw_parallax_m = (
        projected_offset - pose_geometry_value.zero_parallax_offset_m
    )
    baseline_vector = subtract(
        pose_geometry_value.eye_right, pose_geometry_value.eye_left
    )
    baseline = abs(dot(baseline_vector, screen.right))
    if baseline <= EPSILON:
        baseline = max(
            abs(dot(baseline_vector, screen.up)),
            project.stereo.interocular_distance_m,
        )

    pixel_pitch = screen.pixel_pitch_m
    raw_parallax_px = raw_parallax_m / pixel_pitch
    display_parallax_px = _quantize_pixel(raw_parallax_px)

    into_scene = scale(screen.normal, -1.0)
    nominal_depth = dot(
        subtract(scene_hit.point, pose.position), into_scene
    )
    if nominal_depth <= EPSILON:
        raise ValueError("scene hit is not in front of the viewing pose")

    zero_distance = screen.zero_parallax_distance_m
    depth_denominator = (
        1.0 / zero_distance
        - raw_parallax_m
        / (baseline * pose_geometry_value.screen_distance_m)
    )
    if depth_denominator > 1e-12:
        reconstructed_depth = 1.0 / depth_denominator
    else:
        reconstructed_depth = None

    return StereoProjection(
        raw_parallax_px=canonical_float(raw_parallax_px),
        display_parallax_px=float(display_parallax_px),
        reconstructed_depth_m=(
            canonical_float(reconstructed_depth)
            if reconstructed_depth is not None
            else None
        ),
        nominal_depth_m=canonical_float(nominal_depth),
        screen_distance_m=canonical_float(
            pose_geometry_value.screen_distance_m
        ),
        raw_parallax_m=canonical_float(raw_parallax_m),
        zero_parallax_offset_m=canonical_float(
            pose_geometry_value.zero_parallax_offset_m
        ),
        effective_baseline_m=canonical_float(baseline),
    )


def _quantize_pixel(value: float) -> int:
    """Round half away from zero so the display grid is deterministic."""

    if value >= 0.0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def projected_screen_coordinates(
    project: Project,
    pose: ViewingPose,
    point: Vec3,
) -> tuple[float, float] | None:
    """Project a world point to normalised screen coordinates (u, v)."""

    hit = _project_to_screen_plane(pose.position, point, project.screen)
    if hit is None:
        return None
    relative = subtract(hit, project.screen.center)
    horizontal = dot(relative, project.screen.right) / project.screen.width_m
    vertical = dot(relative, project.screen.up) / project.screen.height_m
    return horizontal + 0.5, vertical + 0.5


def radial_scale_from_pose(
    origin: Vec3,
    point: Vec3,
    factor: float,
) -> Vec3:
    return add(origin, scale(subtract(point, origin), factor))


def point_distance_2d(left: tuple[float, float], right: tuple[float, float]) -> float:
    return math.hypot(left[0] - right[0], left[1] - right[1])
