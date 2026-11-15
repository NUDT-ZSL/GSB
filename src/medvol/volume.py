"""Validated 3-D scalar volume and deterministic loading helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .errors import BusinessCode, VolumeRenderError


def _vec3(value: tuple[float, float, float] | list[float] | np.ndarray, name: str) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64)
    if array.shape != (3,):
        raise VolumeRenderError(
            BusinessCode.E_VOLUME_SHAPE,
            f"{name} must contain exactly three numbers",
            details={"field": name, "value": array.tolist()},
        )
    if not np.all(np.isfinite(array)) or np.any(array <= 0):
        raise VolumeRenderError(
            BusinessCode.E_VOLUME_SHAPE,
            f"{name} must contain finite positive values",
            details={"field": name, "value": array.tolist()},
        )
    return array


@dataclass(frozen=True)
class Volume:
    """A scalar volume with NumPy layout ``(z, y, x)``.

    ``spacing`` and ``origin`` are expressed in world units in ``(x, y, z)``
    order. Trilinear interpolation occurs between voxel centers, so the world
    AABB is ``origin .. origin + (shape_xyz - 1) * spacing``.
    """

    data: np.ndarray
    spacing: tuple[float, float, float] = (1.0, 1.0, 1.0)
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        array = np.asarray(self.data)
        if array.ndim != 3:
            raise VolumeRenderError(
                BusinessCode.E_VOLUME_SHAPE,
                "Volume data must be a three-dimensional array",
                details={"shape": list(array.shape)},
            )
        if min(array.shape) < 2:
            raise VolumeRenderError(
                BusinessCode.E_VOLUME_SHAPE,
                "Every volume axis must contain at least two voxels",
                details={"shape": list(array.shape)},
            )
        if array.dtype.kind not in "uif":
            raise VolumeRenderError(
                BusinessCode.E_VOLUME_SHAPE,
                "Volume data must have an integer or floating-point dtype",
                details={"dtype": str(array.dtype)},
            )
        if array.dtype.kind == "f" and not np.all(np.isfinite(array)):
            raise VolumeRenderError(
                BusinessCode.E_VOLUME_SHAPE,
                "Volume data contains NaN or infinity",
                details={"dtype": str(array.dtype), "shape": list(array.shape)},
            )

        spacing = _vec3(self.spacing, "spacing")
        origin = np.asarray(self.origin, dtype=np.float64)
        if origin.shape != (3,) or not np.all(np.isfinite(origin)):
            raise VolumeRenderError(
                BusinessCode.E_VOLUME_SHAPE,
                "origin must be a finite three-vector",
                details={"origin": origin.tolist()},
            )

        # A canonical little-endian C-order copy is required for stable hashes.
        if array.dtype.byteorder == ">":
            array = array.astype(array.dtype.newbyteorder("<"), copy=True)
        else:
            array = np.ascontiguousarray(array)
        array.setflags(write=False)
        spacing.setflags(write=False)
        origin.setflags(write=False)
        object.__setattr__(self, "data", array)
        object.__setattr__(self, "spacing", tuple(float(v) for v in spacing))
        object.__setattr__(self, "origin", tuple(float(v) for v in origin))

    @property
    def shape_zyx(self) -> tuple[int, int, int]:
        return tuple(int(v) for v in self.data.shape)  # type: ignore[return-value]

    @property
    def shape_xyz(self) -> tuple[int, int, int]:
        depth, height, width = self.data.shape
        return int(width), int(height), int(depth)

    @property
    def bounds_min(self) -> np.ndarray:
        return np.asarray(self.origin, dtype=np.float64)

    @property
    def bounds_max(self) -> np.ndarray:
        return self.bounds_min + (np.asarray(self.shape_xyz, dtype=np.float64) - 1.0) * np.asarray(
            self.spacing, dtype=np.float64
        )

    @property
    def center(self) -> np.ndarray:
        return 0.5 * (self.bounds_min + self.bounds_max)

    @property
    def diagonal(self) -> float:
        return float(np.linalg.norm(self.bounds_max - self.bounds_min))

    @property
    def value_range(self) -> tuple[float, float]:
        return float(np.min(self.data)), float(np.max(self.data))

    @property
    def min_spacing(self) -> float:
        return float(min(self.spacing))

    @property
    def sha256(self) -> str:
        digest = hashlib.sha256()
        digest.update(np.asarray(self.shape_zyx, dtype="<i8").tobytes())
        digest.update(np.asarray(self.spacing, dtype="<f8").tobytes())
        digest.update(np.asarray(self.origin, dtype="<f8").tobytes())
        digest.update(self.data.dtype.str.encode("ascii"))
        digest.update(self.data.tobytes(order="C"))
        return digest.hexdigest()

    @classmethod
    def load_npy(
        cls,
        path: str | Path,
        *,
        spacing: tuple[float, float, float] = (1.0, 1.0, 1.0),
        origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
    ) -> "Volume":
        array = np.load(Path(path), allow_pickle=False)
        return cls(array, spacing=spacing, origin=origin)

    def metadata(self) -> dict[str, Any]:
        vmin, vmax = self.value_range
        return {
            "shape_zyx": list(self.shape_zyx),
            "shape_xyz": list(self.shape_xyz),
            "dtype": str(self.data.dtype),
            "spacing": list(self.spacing),
            "origin": list(self.origin),
            "bounds_min": self.bounds_min.tolist(),
            "bounds_max": self.bounds_max.tolist(),
            "value_range": [vmin, vmax],
            "sha256": self.sha256,
        }

    def metadata_json(self) -> str:
        return json.dumps(self.metadata(), sort_keys=True, separators=(",", ":"), allow_nan=False)

