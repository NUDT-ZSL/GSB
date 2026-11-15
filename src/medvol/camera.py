"""Perspective camera and deterministic ray generation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import BusinessCode, VolumeRenderError


RAY_DIRECTION_EPSILON = 1.0e-12


def _as_vector3(value: Any, name: str) -> np.ndarray:
    vector = np.asarray(value, dtype=np.float64)
    if vector.shape != (3,):
        raise VolumeRenderError(
            BusinessCode.E_RAY_DIR,
            f"{name} must contain exactly three components",
            details={"field": name},
        )
    if not np.all(np.isfinite(vector)):
        raise VolumeRenderError(
            BusinessCode.E_RAY_DIR,
            f"{name} contains NaN or infinity",
            details={"field": name},
        )
    return vector


def validate_ray_directions(
    directions: np.ndarray,
    *,
    zero_epsilon: float = RAY_DIRECTION_EPSILON,
) -> None:
    """Reject any zero-length ray direction with ``E_RAY_DIR``."""

    array = np.asarray(directions, dtype=np.float64)
    if array.ndim < 1 or array.shape[-1] != 3:
        raise VolumeRenderError(
            BusinessCode.E_RAY_DIR,
            "Ray directions must have a final dimension of length three",
            details={"shape": list(array.shape)},
        )
    if not np.all(np.isfinite(array)):
        raise VolumeRenderError(
            BusinessCode.E_RAY_DIR,
            "Ray directions contain NaN or infinity",
            details={"shape": list(array.shape)},
        )
    lengths = np.linalg.norm(array.reshape(-1, 3), axis=1)
    zero_count = int(np.count_nonzero(lengths <= zero_epsilon))
    if zero_count:
        raise VolumeRenderError(
            BusinessCode.E_RAY_DIR,
            "Ray direction must be a non-zero vector",
            details={"zero_direction_count": zero_count, "zero_epsilon": zero_epsilon},
        )


@dataclass(frozen=True)
class Camera:
    position: tuple[float, float, float]
    target: tuple[float, float, float]
    up: tuple[float, float, float] = (0.0, 1.0, 0.0)
    fov_y_deg: float = 40.0

    def __post_init__(self) -> None:
        position = _as_vector3(self.position, "position")
        target = _as_vector3(self.target, "target")
        up = _as_vector3(self.up, "up")

        forward = target - position
        forward_norm = float(np.linalg.norm(forward))
        if forward_norm <= RAY_DIRECTION_EPSILON:
            raise VolumeRenderError(
                BusinessCode.E_RAY_DIR,
                "Camera view ray direction is zero",
                details={"position": position.tolist(), "target": target.tolist()},
            )
        forward /= forward_norm

        right = np.cross(forward, up)
        if float(np.linalg.norm(right)) <= RAY_DIRECTION_EPSILON:
            raise VolumeRenderError(
                BusinessCode.E_RAY_DIR,
                "Camera up vector is parallel to the view direction",
                details={"forward": forward.tolist(), "up": up.tolist()},
            )
        right /= np.linalg.norm(right)
        true_up = np.cross(right, forward)
        true_up /= np.linalg.norm(true_up)

        if not math.isfinite(self.fov_y_deg) or not 0.0 < self.fov_y_deg < 179.0:
            raise VolumeRenderError(
                BusinessCode.E_RAY_DIR,
                "fov_y_deg must be finite and between 0 and 179 degrees",
                details={"fov_y_deg": self.fov_y_deg},
            )

        # Store finite tuples to keep the dataclass immutable and hashable.
        object.__setattr__(self, "position", tuple(float(v) for v in position))
        object.__setattr__(self, "target", tuple(float(v) for v in target))
        object.__setattr__(self, "up", tuple(float(v) for v in true_up))

    @classmethod
    def look_at(
        cls,
        position: tuple[float, float, float] | np.ndarray,
        target: tuple[float, float, float] | np.ndarray,
        up: tuple[float, float, float] | np.ndarray = (0.0, 1.0, 0.0),
        fov_y_deg: float = 40.0,
    ) -> "Camera":
        return cls(tuple(float(v) for v in position), tuple(float(v) for v in target), tuple(float(v) for v in up), fov_y_deg)

    @property
    def forward(self) -> np.ndarray:
        direction = np.asarray(self.target) - np.asarray(self.position)
        return direction / np.linalg.norm(direction)

    @property
    def right(self) -> np.ndarray:
        direction = np.cross(self.forward, np.asarray(self.up))
        return direction / np.linalg.norm(direction)

    @property
    def true_up(self) -> np.ndarray:
        direction = np.cross(self.right, self.forward)
        return direction / np.linalg.norm(direction)

    def ray_directions(self, width: int, height: int) -> np.ndarray:
        if width <= 0 or height <= 0:
            raise VolumeRenderError(
                BusinessCode.E_RAY_DIR,
                "Render resolution must be positive",
                details={"width": width, "height": height},
            )
        width = int(width)
        height = int(height)
        aspect = width / height
        tan_half = math.tan(math.radians(self.fov_y_deg) * 0.5)

        # Pixel centers, with row 0 at the top of the image.
        screen_x = (2.0 * (np.arange(width, dtype=np.float64) + 0.5) / width - 1.0) * aspect * tan_half
        screen_y = (1.0 - 2.0 * (np.arange(height, dtype=np.float64) + 0.5) / height) * tan_half
        sx, sy = np.meshgrid(screen_x, screen_y)

        directions = (
            self.forward[None, :]
            + sx.reshape(-1, 1) * self.right[None, :]
            + sy.reshape(-1, 1) * self.true_up[None, :]
        )
        lengths = np.linalg.norm(directions, axis=1)
        if np.any(lengths <= RAY_DIRECTION_EPSILON):
            raise VolumeRenderError(
                BusinessCode.E_RAY_DIR,
                "Generated a zero-length camera ray",
                details={"zero_direction_count": int(np.count_nonzero(lengths <= RAY_DIRECTION_EPSILON))},
            )
        directions /= lengths[:, None]
        return directions

    def metadata(self) -> dict[str, Any]:
        return {
            "position": list(self.position),
            "target": list(self.target),
            "up": list(self.up),
            "forward": self.forward.tolist(),
            "fov_y_deg": float(self.fov_y_deg),
        }

