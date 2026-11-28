from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

DEPTH_SAMPLES = (0.0, 0.25, 0.5, 0.75, 1.0)
QUATERNION_EPSILON = 1.0e-12


def _canonical_component(values: np.ndarray, tolerance: float = 1.0e-12) -> int:
    for index, value in enumerate(values):
        if abs(float(value)) > tolerance:
            return index
    return 0


def canonical_quaternion(quaternion: np.ndarray) -> np.ndarray:
    """Return a unit quaternion in canonical ``w, x, y, z`` form."""
    q = np.asarray(quaternion, dtype=np.float64).copy()
    if q.shape != (4,):
        raise ValueError("quaternion must have four components")
    norm = float(np.linalg.norm(q))
    if not np.isfinite(norm) or norm <= QUATERNION_EPSILON:
        raise ValueError("quaternion norm is zero or non-finite")
    q /= norm
    if q[0] < -1.0e-12:
        q = -q
    elif abs(q[0]) <= 1.0e-12:
        index = _canonical_component(q[1:])
        if q[index + 1] < 0.0:
            q = -q
    return q


def quaternion_to_matrix(quaternion: np.ndarray) -> np.ndarray:
    w, x, y, z = canonical_quaternion(quaternion)
    return np.array(
        [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
            [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
            [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def matrix_to_quaternion(matrix: np.ndarray) -> np.ndarray:
    """Convert a proper rotation matrix to a canonical unit quaternion."""
    m = np.asarray(matrix, dtype=np.float64)
    if m.shape != (3, 3):
        raise ValueError("rotation matrix must be 3x3")
    trace = float(np.trace(m))
    if trace > 0.0:
        s = np.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (m[2, 1] - m[1, 2]) / s
        y = (m[0, 2] - m[2, 0]) / s
        z = (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        w = (m[2, 1] - m[1, 2]) / s
        x = 0.25 * s
        y = (m[0, 1] + m[1, 0]) / s
        z = (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        w = (m[0, 2] - m[2, 0]) / s
        x = (m[0, 1] + m[1, 0]) / s
        y = 0.25 * s
        z = (m[1, 2] + m[2, 1]) / s
    else:
        s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
        w = (m[1, 0] - m[0, 1]) / s
        x = (m[0, 2] + m[2, 0]) / s
        y = (m[1, 2] + m[2, 1]) / s
        z = 0.25 * s
    return canonical_quaternion(np.array([w, x, y, z], dtype=np.float64))


def euler_xyz_deg_to_matrix(angles: np.ndarray) -> np.ndarray:
    """Return Rz @ Ry @ Rx for an XYZ Euler tuple expressed in degrees."""
    x, y, z = np.deg2rad(np.asarray(angles, dtype=np.float64))
    cx, sx = np.cos(x), np.sin(x)
    cy, sy = np.cos(y), np.sin(y)
    cz, sz = np.cos(z), np.sin(z)
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cx, -sx], [0.0, sx, cx]])
    ry = np.array([[cy, 0.0, sy], [0.0, 1.0, 0.0], [-sy, 0.0, cy]])
    rz = np.array([[cz, -sz, 0.0], [sz, cz, 0.0], [0.0, 0.0, 1.0]])
    return rz @ ry @ rx


def matrix_to_euler_xyz_deg(matrix: np.ndarray) -> np.ndarray:
    """Inverse of :func:`euler_xyz_deg_to_matrix`, with gimbal fallback."""
    m = np.asarray(matrix, dtype=np.float64)
    sy = -float(m[2, 0])
    sy = float(np.clip(sy, -1.0, 1.0))
    y = np.arcsin(sy)
    if abs(np.cos(y)) > 1.0e-10:
        x = np.arctan2(float(m[2, 1]), float(m[2, 2]))
        z = np.arctan2(float(m[1, 0]), float(m[0, 0]))
    else:
        x = np.arctan2(-float(m[1, 2]), float(m[1, 1]))
        z = 0.0
    return np.rad2deg(np.array([x, y, z], dtype=np.float64))


def matrix_from_rotation_scale(rotation: np.ndarray, scale: np.ndarray) -> np.ndarray:
    r = np.asarray(rotation, dtype=np.float64)
    s = np.asarray(scale, dtype=np.float64)
    if r.shape != (3, 3) or s.shape != (3,):
        raise ValueError("rotation must be 3x3 and scale must have three components")
    if not np.all(np.isfinite(r)) or not np.all(np.isfinite(s)):
        raise ValueError("rotation and scale must be finite")
    if np.any(s <= 0.0):
        raise ValueError("all Gaussian scales must be positive")
    return r @ np.diag(s * s) @ r.T


def covariance_from_6(values: np.ndarray, order: str = "xx,xy,xz,yy,yz,zz") -> np.ndarray:
    """Decode a six-component symmetric covariance with an explicit packing order."""
    raw = np.asarray(values, dtype=np.float64)
    if raw.shape != (6,):
        raise ValueError("covariance must have six components")
    normalized_order = "".join(character.lower() for character in order if character.isalpha())
    if normalized_order in {"xxxyxzyyyzzz", "xxxyxzyyyyzzz"}:
        xx, xy, xz, yy, yz, zz = raw
    elif normalized_order == "xxyyzzxyxzyz":
        xx, yy, zz, xy, xz, yz = raw
    elif normalized_order == "xxyyxyxzyzzz":
        xx, yy, xy, xz, yz, zz = raw
    else:
        raise ValueError(f"unsupported covariance packing order: {order!r}")
    return np.array(
        [[xx, xy, xz], [xy, yy, yz], [xz, yz, zz]], dtype=np.float64
    )


def covariance_to_6(covariance: np.ndarray) -> list[float]:
    c = np.asarray(covariance, dtype=np.float64)
    return [
        float(c[0, 0]),
        float(c[0, 1]),
        float(c[0, 2]),
        float(c[1, 1]),
        float(c[1, 2]),
        float(c[2, 2]),
    ]


def decompose_covariance(covariance: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return principal axes, scales, and quaternion in a deterministic order."""
    c = np.asarray(covariance, dtype=np.float64)
    if c.shape != (3, 3):
        raise ValueError("covariance must be 3x3")
    if not np.all(np.isfinite(c)):
        raise ValueError("covariance must be finite")
    if np.max(np.abs(c - c.T)) > 1.0e-9:
        raise ValueError("covariance is not symmetric")
    symmetric = 0.5 * (c + c.T)
    eigenvalues, eigenvectors = np.linalg.eigh(symmetric)
    if float(np.min(eigenvalues)) < -1.0e-9:
        raise ValueError("covariance is not positive semidefinite")
    eigenvalues = np.maximum(eigenvalues, 0.0)
    order = np.argsort(eigenvalues)[::-1]
    eigenvalues = eigenvalues[order]
    axes = eigenvectors[:, order].copy()
    for column in range(3):
        pivot = int(np.argmax(np.abs(axes[:, column])))
        if axes[pivot, column] < 0.0:
            axes[:, column] *= -1.0
    if float(np.linalg.det(axes)) < 0.0:
        axes[:, 2] *= -1.0
    scales = np.sqrt(eigenvalues)
    quaternion = matrix_to_quaternion(axes)
    return axes, scales, quaternion


def canonical_plane_normal(covariance: np.ndarray) -> np.ndarray:
    """Thin-axis normal of an anisotropic Gaussian, used for per-pixel depth."""
    c = np.asarray(covariance, dtype=np.float64)
    eigenvalues, eigenvectors = np.linalg.eigh(0.5 * (c + c.T))
    normal = eigenvectors[:, int(np.argmin(eigenvalues))].copy()
    pivot = int(np.argmax(np.abs(normal)))
    if normal[pivot] < -1.0e-12:
        normal *= -1.0
    return normal / np.linalg.norm(normal)


@dataclass(frozen=True)
class DepthCurve:
    control_points: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if len(self.control_points) < 2:
            raise ValueError("depth curve needs at least two control points")
        previous = -1.0
        for position, gain in self.control_points:
            if not (0.0 <= position <= 1.0) or not np.isfinite(gain):
                raise ValueError("depth curve control points are invalid")
            if position <= previous:
                raise ValueError("depth curve positions must increase")
            previous = position

    def sample(self, normalized_depth: float) -> float:
        d = float(np.clip(normalized_depth, 0.0, 1.0))
        points = self.control_points
        if d <= points[0][0]:
            return float(np.clip(points[0][1], 0.0, 1.0))
        if d >= points[-1][0]:
            return float(np.clip(points[-1][1], 0.0, 1.0))
        for index in range(1, len(points)):
            x0, y0 = points[index - 1]
            x1, y1 = points[index]
            if d <= x1:
                ratio = (d - x0) / (x1 - x0)
                return float(np.clip(y0 + ratio * (y1 - y0), 0.0, 1.0))
        return 0.0

    def as_list(self) -> list[list[float]]:
        return [[float(x), float(y)] for x, y in self.control_points]

    def samples(self) -> dict[str, float]:
        return {f"{d:.2f}": self.sample(d) for d in DEPTH_SAMPLES}


@dataclass(frozen=True)
class NarrativeLayer:
    index: int
    name: str


@dataclass
class GaussianElement:
    element_id: str
    layer_index: int
    layer_name: str
    center: np.ndarray
    covariance: np.ndarray
    opacity: float
    color: np.ndarray
    depth_curve: DepthCurve
    stable_index: int
    source_schema: str
    source_index: int
    rotation: np.ndarray
    scale: np.ndarray
    quaternion: np.ndarray
    plane_normal: np.ndarray

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.element_id,
            "layer_index": int(self.layer_index),
            "layer_name": self.layer_name,
            "center": [float(v) for v in self.center],
            "covariance": covariance_to_6(self.covariance),
            "covariance_encoding": "xx,xy,xz,yy,yz,zz",
            "rotation_matrix": [[float(v) for v in row] for row in self.rotation],
            "rotation_quaternion_wxyz": [float(v) for v in self.quaternion],
            "rotation_euler_xyz_deg": [
                float(v) for v in matrix_to_euler_xyz_deg(self.rotation)
            ],
            "scale": [float(v) for v in self.scale],
            "opacity": float(self.opacity),
            "color": [float(v) for v in self.color],
            "depth_curve": {
                "type": "piecewise_linear",
                "control_points": self.depth_curve.as_list(),
                "samples": self.depth_curve.samples(),
            },
            "plane_normal": [float(v) for v in self.plane_normal],
            "stable_index": int(self.stable_index),
            "source_schema": self.source_schema,
            "source_index": int(self.source_index),
        }


@dataclass
class NarrativeLayerSpec:
    index: int
    name: str
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "name": self.name,
            "aliases": list(self.aliases),
        }


@dataclass(frozen=True)
class CameraFrame:
    frame_index: int
    eye: tuple[float, float, float]
    target: tuple[float, float, float]
    up: tuple[float, float, float]
    width: int = 160
    height: int = 96
    fov_y_deg: float = 50.0
    near: float = 0.08
    far: float = 20.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame": int(self.frame_index),
            "eye": [float(v) for v in self.eye],
            "target": [float(v) for v in self.target],
            "up": [float(v) for v in self.up],
            "width": int(self.width),
            "height": int(self.height),
            "fov_y_deg": float(self.fov_y_deg),
            "near": float(self.near),
            "far": float(self.far),
        }


@dataclass
class SceneBundle:
    scene_id: str
    seed: int
    layers: list[NarrativeLayerSpec]
    camera_frames: list[CameraFrame]
    elements: list[GaussianElement] = field(default_factory=list)

    @property
    def width(self) -> int:
        return int(self.camera_frames[0].width)

    @property
    def height(self) -> int:
        return int(self.camera_frames[0].height)

    def layer_by_name(self) -> dict[str, NarrativeLayerSpec]:
        result: dict[str, NarrativeLayerSpec] = {}
        for layer in self.layers:
            result[layer.name] = layer
            for alias in layer.aliases:
                result[alias] = layer
        return result

    def layer_by_index(self) -> dict[int, NarrativeLayerSpec]:
        return {layer.index: layer for layer in self.layers}
