"""Validated, immutable-ish scene model used by the renderer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

Vec3 = Tuple[float, float, float]


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _mul(a: Vec3, s: float) -> Vec3:
    return (a[0] * s, a[1] * s, a[2] * s)


def _length(a: Vec3) -> float:
    return _dot(a, a) ** 0.5


def _normalize(a: Vec3) -> Vec3:
    length = _length(a)
    if length == 0.0:
        raise ValueError("zero-length vector")
    return (a[0] / length, a[1] / length, a[2] / length)


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


@dataclass(frozen=True)
class MediaBox:
    id: str
    kind: str
    center: Vec3
    size: Vec3
    extinction: Vec3
    albedo: Vec3
    ior: float
    declaration_index: int

    @property
    def minimum(self) -> Vec3:
        return (
            self.center[0] - self.size[0] * 0.5,
            self.center[1] - self.size[1] * 0.5,
            self.center[2] - self.size[2] * 0.5,
        )

    @property
    def maximum(self) -> Vec3:
        return (
            self.center[0] + self.size[0] * 0.5,
            self.center[1] + self.size[1] * 0.5,
            self.center[2] + self.size[2] * 0.5,
        )

    @property
    def priority_key(self) -> tuple[float, int]:
        # Higher IOR wins. Equal IOR is resolved by the earlier declaration.
        return (self.ior, -self.declaration_index)

    @property
    def extinction_scalar(self) -> float:
        return 0.2126 * self.extinction[0] + 0.7152 * self.extinction[1] + 0.0722 * self.extinction[2]

    @property
    def scattering_scalar(self) -> float:
        return (
            0.2126 * self.extinction[0] * self.albedo[0]
            + 0.7152 * self.extinction[1] * self.albedo[1]
            + 0.0722 * self.extinction[2] * self.albedo[2]
        )

    def contains(self, point: Vec3, epsilon: float = 0.0) -> bool:
        lo = self.minimum
        hi = self.maximum
        return (
            lo[0] - epsilon <= point[0] <= hi[0] + epsilon
            and lo[1] - epsilon <= point[1] <= hi[1] + epsilon
            and lo[2] - epsilon <= point[2] <= hi[2] + epsilon
        )


@dataclass(frozen=True)
class PointLight:
    id: str
    position: Vec3
    intensity: float


@dataclass(frozen=True)
class Camera:
    position: Vec3
    forward: Vec3
    up: Vec3
    vertical_fov_deg: float
    aspect_ratio: float


@dataclass(frozen=True)
class RenderConfig:
    width: int
    height: int
    samples_per_pixel: int
    step_size: float
    max_steps: int
    transmittance_cutoff: float
    fixed_scale: int
    transmittance_tolerance: float
    tile_width: int
    tile_height: int
    max_workers: int
    inflight_memory_limit_bytes: int
    state_dir: Path
    output_dir: Path
    ray_epsilon: float
    exposure: float


@dataclass(frozen=True)
class JitterConfig:
    sample_offsets: tuple[tuple[float, float], ...]
    step_scales: tuple[float, ...]
    cutoff_scales: tuple[float, ...]


@dataclass(frozen=True)
class Scene:
    name: str
    seed: int | None
    bounds_min: Vec3
    bounds_max: Vec3
    media: tuple[MediaBox, ...]
    lights: tuple[PointLight, ...]
    camera: Camera
    render: RenderConfig
    jitter: JitterConfig
    source_path: Path

