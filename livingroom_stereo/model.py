from __future__ import annotations

from dataclasses import dataclass
from typing import Any

Vec3 = tuple[float, float, float]
Vec2 = tuple[float, float]


@dataclass(frozen=True)
class Room:
    width_m: float
    depth_m: float
    height_m: float

    @property
    def minimum(self) -> Vec3:
        return (0.0, 0.0, 0.0)

    @property
    def maximum(self) -> Vec3:
        return (self.width_m, self.depth_m, self.height_m)

    @property
    def center(self) -> Vec3:
        return (
            self.width_m / 2.0,
            self.depth_m / 2.0,
            self.height_m / 2.0,
        )


@dataclass(frozen=True)
class Screen:
    id: str
    center: Vec3
    width_m: float
    height_m: float
    resolution: tuple[int, int]
    blocks: tuple[int, int]
    samples_per_block: tuple[int, int]
    right: Vec3
    up: Vec3
    normal: Vec3
    design_viewing_distance_m: float
    zero_parallax_distance_m: float

    @property
    def pixel_pitch_m(self) -> float:
        return self.width_m / self.resolution[0]

    @property
    def sample_grid(self) -> tuple[int, int]:
        return (
            self.blocks[0] * self.samples_per_block[0],
            self.blocks[1] * self.samples_per_block[1],
        )


@dataclass(frozen=True)
class StereoConfig:
    interocular_distance_m: float


@dataclass(frozen=True)
class AssessmentConfig:
    comfort_parallax_min_px: float
    comfort_parallax_max_px: float
    depth_distortion_threshold: float
    overflow_ratio_weight: float
    overflow_magnitude_weight: float
    min_comfortable_block_ratio: float
    viewing_zone_radius_m: float


@dataclass(frozen=True)
class Furniture:
    id: str
    kind: str
    minimum: Vec3
    maximum: Vec3

    @property
    def center(self) -> Vec3:
        return tuple(
            (self.minimum[index] + self.maximum[index]) / 2.0
            for index in range(3)
        )

    @property
    def corners(self) -> tuple[Vec3, ...]:
        xs = (self.minimum[0], self.maximum[0])
        ys = (self.minimum[1], self.maximum[1])
        zs = (self.minimum[2], self.maximum[2])
        return tuple(
            (x_coord, y_coord, z_coord)
            for x_coord in xs
            for y_coord in ys
            for z_coord in zs
        )

    def contains(self, point: Vec3, tolerance: float = 1e-9) -> bool:
        return all(
            self.minimum[index] - tolerance
            <= point[index]
            <= self.maximum[index] + tolerance
            for index in range(3)
        )


@dataclass(frozen=True)
class ViewingPose:
    id: str
    position: Vec3
    forward: Vec3
    up: Vec3
    furniture_id: str


@dataclass(frozen=True)
class Relationship:
    id: str
    from_ref: str
    to_ref: str


@dataclass(frozen=True)
class AcousticSample:
    id: str
    position: Vec2
    coverage_radius_m: float
    viewing_pose_id: str | None


@dataclass(frozen=True)
class Project:
    source_path: str
    raw: dict[str, Any]
    fingerprint: str
    schema_version: int
    project_id: str
    seed: int | None
    room: Room
    screen: Screen
    stereo: StereoConfig
    assessment: AssessmentConfig
    viewing_poses: tuple[ViewingPose, ...]
    furniture: tuple[Furniture, ...]
    relationships: tuple[Relationship, ...]
    acoustic_samples: tuple[AcousticSample, ...]

    def pose_by_id(self) -> dict[str, ViewingPose]:
        return {pose.id: pose for pose in self.viewing_poses}

    def furniture_by_id(self) -> dict[str, Furniture]:
        return {item.id: item for item in self.furniture}
