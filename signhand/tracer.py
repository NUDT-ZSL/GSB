"""Adaptive sphere tracing, surface normals and Lambert rendering."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .math3d import normalize
from .project import CameraSpec, Project, RenderSettings
from .sdf import SDFScene


TRACE_MISS = 0
TRACE_HIT = 1
TRACE_ERR_MAX_STEPS = 2
TRACE_ERR_HIT_BEYOND_FAR_PLANE = 3

TRACE_STATUS_NAMES = {
    TRACE_MISS: "MISS",
    TRACE_HIT: "HIT",
    TRACE_ERR_MAX_STEPS: "ERR_TRACE_MAX_STEPS",
    TRACE_ERR_HIT_BEYOND_FAR_PLANE: "ERR_HIT_BEYOND_FAR_PLANE",
}


def camera_rays(camera: CameraSpec, width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    """Generate one normalized ray per image pixel."""

    if width <= 0 or height <= 0:
        raise ValueError("image dimensions must be positive")
    position = camera.position
    target = camera.target
    forward = normalize(target - position)
    if np.linalg.norm(np.cross(forward, camera.up)) <= 1e-12:
        raise ValueError("camera up vector is parallel to its viewing direction")
    right = normalize(np.cross(forward, camera.up))
    true_up = normalize(np.cross(right, forward))

    aspect = width / height
    pixel_x = (np.arange(width, dtype=np.float64) + 0.5) / width * 2.0 - 1.0
    pixel_y = 1.0 - (np.arange(height, dtype=np.float64) + 0.5) / height * 2.0
    grid_x, grid_y = np.meshgrid(pixel_x, pixel_y)
    offsets = grid_x.reshape(-1)[:, None] * right + grid_y.reshape(-1)[:, None] * true_up

    if camera.mode == "orthographic":
        origins = (
            position
            + grid_x.reshape(-1)[:, None] * camera.half_width * right
            + grid_y.reshape(-1)[:, None] * camera.half_height * true_up
        )
        directions = np.broadcast_to(forward, origins.shape).copy()
    else:
        tangent = np.tan(np.deg2rad(camera.fov_deg) * 0.5)
        directions = (
            forward
            + right * (grid_x.reshape(-1)[:, None] * tangent * aspect)
            + true_up * (grid_y.reshape(-1)[:, None] * tangent)
        )
        directions = normalize(directions)
        origins = np.broadcast_to(position, directions.shape).copy()
    return origins, directions


@dataclass(frozen=True)
class TraceResult:
    hit_t: np.ndarray
    status: np.ndarray
    normals: np.ndarray
    primitive_ids: np.ndarray
    origins: np.ndarray
    directions: np.ndarray
    width: int
    height: int

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for code, name in TRACE_STATUS_NAMES.items():
            counts[name] = int(np.sum(self.status == code))
        return counts

    @property
    def hit_points(self) -> np.ndarray:
        points = np.full_like(self.origins, np.nan)
        mask = self.status == TRACE_HIT
        points[mask] = self.origins[mask] + self.hit_t[mask, None] * self.directions[mask]
        return points


class SphereTracer:
    def __init__(
        self,
        scene: SDFScene,
        camera: CameraSpec,
        settings: RenderSettings,
    ):
        self.scene = scene
        self.camera = camera
        self.settings = settings

    def trace_rays(self, origins, directions, width: int | None = None, height: int | None = None) -> TraceResult:
        origins = np.asarray(origins, dtype=np.float64)
        directions = np.asarray(directions, dtype=np.float64)
        if origins.ndim != 2 or origins.shape[1] != 3:
            raise ValueError("ray origins must have shape (N, 3)")
        if directions.shape != origins.shape:
            raise ValueError("ray directions must match origins")
        norm = np.linalg.norm(directions, axis=1)
        if np.any(np.abs(norm - 1.0) > 1e-8):
            directions = directions / np.maximum(norm[:, None], 1e-12)

        count = origins.shape[0]
        settings = self.settings
        near, far = float(self.camera.near), float(self.camera.far)
        t = np.full(count, near, dtype=np.float64)
        status = np.full(count, -1, dtype=np.int8)
        hit_t = np.full(count, np.nan, dtype=np.float64)

        scene_exit = self.scene.ray_aabb_tmax(origins, directions)
        trace_limit = np.where(
            np.isfinite(scene_exit) & (scene_exit > far),
            scene_exit + max(settings.max_step, settings.epsilon),
            far,
        )
        trace_limit = np.maximum(trace_limit, far)
        invalid = near >= trace_limit
        status[invalid] = TRACE_MISS

        active = status == -1
        previous_t = np.full(count, np.nan, dtype=np.float64)
        previous_distance = np.full(count, np.inf, dtype=np.float64)

        for _ in range(settings.max_steps):
            active = status == -1
            if not np.any(active):
                break
            points = origins + t[:, None] * directions
            distance = self.scene.distance(points)

            near_surface = active & (distance <= settings.epsilon)
            if np.any(near_surface):
                denominator = previous_distance[near_surface] - distance[near_surface]
                interpolated = np.where(
                    np.isfinite(previous_t[near_surface])
                    & (previous_distance[near_surface] > 0.0)
                    & (np.abs(denominator) > 1e-14),
                    previous_t[near_surface]
                    + previous_distance[near_surface]
                    * (t[near_surface] - previous_t[near_surface])
                    / denominator,
                    t[near_surface],
                )
                interpolated = np.clip(interpolated, near, t[near_surface])
                beyond_far = interpolated > far + 1e-10
                hit_indices = np.flatnonzero(near_surface)
                hit_t[hit_indices[~beyond_far]] = interpolated[~beyond_far]
                status[hit_indices[~beyond_far]] = TRACE_HIT
                status[hit_indices[beyond_far]] = TRACE_ERR_HIT_BEYOND_FAR_PLANE

            active = status == -1
            reached_limit = active & (t >= trace_limit - 1e-12)
            status[reached_limit] = TRACE_MISS

            active = status == -1
            if not np.any(active):
                break
            remaining = np.maximum(trace_limit - t, 0.0)
            step = np.clip(
                settings.step_safety * np.maximum(distance, 0.0),
                settings.min_step,
                settings.max_step,
            )
            step = np.minimum(step, remaining)
            new_t = t + step
            no_progress = active & (new_t <= t) & (distance > settings.epsilon)
            if np.any(no_progress):
                # A numerically stalled ray is reported as a max-step failure
                # instead of being painted as empty background.
                status[no_progress] = TRACE_ERR_MAX_STEPS
            previous_t = np.where(active, t, previous_t)
            previous_distance = np.where(active, distance, previous_distance)
            t = np.where(active, np.minimum(new_t, trace_limit), t)

        remaining_active = status == -1
        if np.any(remaining_active):
            status[remaining_active & (t >= trace_limit - 1e-12)] = TRACE_MISS
            status[remaining_active & (t < trace_limit - 1e-12)] = TRACE_ERR_MAX_STEPS

        hit_mask = status == TRACE_HIT
        normals = np.zeros_like(origins)
        primitive_ids = np.full(count, "", dtype=object)
        if np.any(hit_mask):
            hit_points = origins[hit_mask] + hit_t[hit_mask, None] * directions[hit_mask]
            normals[hit_mask] = self.scene.gradient(hit_points, settings.gradient_epsilon)
            ids, _ = self.scene.nearest_primitive(hit_points)
            primitive_ids[hit_mask] = ids
        return TraceResult(
            hit_t=hit_t,
            status=status,
            normals=normals,
            primitive_ids=primitive_ids,
            origins=origins,
            directions=directions,
            width=int(width if width is not None else np.sqrt(count)),
            height=int(height if height is not None else 0),
        )

    def render(self) -> tuple[np.ndarray, TraceResult]:
        origins, directions = camera_rays(self.camera, self.settings.width, self.settings.height)
        result = self.trace_rays(
            origins,
            directions,
            width=self.settings.width,
            height=self.settings.height,
        )
        return self.shade(result), result

    def trace_image_rays(self) -> TraceResult:
        origins, directions = camera_rays(self.camera, self.settings.width, self.settings.height)
        return self.trace_rays(
            origins,
            directions,
            width=self.settings.width,
            height=self.settings.height,
        )

    def shade(self, result: TraceResult) -> np.ndarray:
        width, height = self.settings.width, self.settings.height
        image = np.empty((height, width, 3), dtype=np.float64)
        vertical = np.linspace(0.0, 1.0, height, dtype=np.float64)[:, None, None]
        background = (
            self.settings.background_top
            * (1.0 - vertical)
            + self.settings.background_bottom * vertical
        )
        image[:] = background

        flat_image = image.reshape(-1, 3)
        error_colors = {
            TRACE_ERR_MAX_STEPS: np.array([0.95, 0.05, 0.65], dtype=np.float64),
            TRACE_ERR_HIT_BEYOND_FAR_PLANE: np.array([0.05, 0.85, 0.95], dtype=np.float64),
        }
        for code, color in error_colors.items():
            flat_image[result.status == code] = color

        hit_mask = result.status == TRACE_HIT
        if np.any(hit_mask):
            light = normalize(self.settings.light_direction)
            normals = result.normals[hit_mask]
            diffuse = np.clip(normals @ light, 0.0, 1.0)
            intensity = self.settings.ambient + (1.0 - self.settings.ambient) * diffuse
            palette = {}
            for primitive in self.scene.primitives:
                palette[primitive.id] = primitive.color
            hit_ids = result.primitive_ids[hit_mask]
            base = np.stack(
                [palette.get(primitive_id, np.array([0.8, 0.7, 0.6])) for primitive_id in hit_ids]
            )
            shaded = np.clip(base * intensity[:, None], 0.0, 1.0)
            flat_image[hit_mask] = shaded
        image = np.clip(flat_image.reshape(height, width, 3), 0.0, 1.0)
        return np.floor(image * 255.0 + 0.5).astype(np.uint8)


def render_frame(
    project: Project,
    scene: SDFScene,
    settings: RenderSettings | None = None,
) -> tuple[np.ndarray, TraceResult]:
    return SphereTracer(scene, project.camera, settings or project.render).render()

