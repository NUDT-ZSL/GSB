"""Homography derivation and deterministic normalized-coordinate models."""

from __future__ import annotations

import numpy as np

from .errors import BusinessError, ErrorCode
from .model import DistortionStage, SceneParameters, ViewParameters, qtuple


def _extrinsic_parts(extrinsic: tuple[float, ...]) -> tuple[np.ndarray, np.ndarray]:
    if len(extrinsic) != 12:
        raise BusinessError(
            ErrorCode.FORMAT_CORRUPT,
            f"extrinsic must have 12 values, got {len(extrinsic)}",
            stage="parse",
            field_path="extrinsic",
        )
    matrix = np.asarray(extrinsic, dtype=np.float64).reshape(3, 4)
    return matrix[:, :3], matrix[:, 3]


def derive_homography(
    focal: float,
    principal_x: float,
    principal_y: float,
    extrinsic: tuple[float, ...],
    canvas_width: int,
    canvas_height: int,
) -> tuple[float, ...]:
    """Derive undistorted-view-pixel to panorama-pixel homography.

    Convention: the row-major 3x4 extrinsic maps a world point to the camera.
    The world plane is Z=0. The panorama plane uses a pinhole camera with the
    view focal length and canvas center as its principal point.
    """

    rotation, translation = _extrinsic_parts(extrinsic)
    intrinsic = np.array(
        [
            [focal, 0.0, principal_x],
            [0.0, focal, principal_y],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    panorama_intrinsic = np.array(
        [
            [focal, 0.0, (canvas_width - 1) / 2.0],
            [0.0, focal, (canvas_height - 1) / 2.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    plane_to_view = intrinsic @ np.column_stack(
        (rotation[:, 0], rotation[:, 1], translation)
    )
    try:
        view_to_plane = np.linalg.inv(plane_to_view)
    except np.linalg.LinAlgError as exc:
        raise BusinessError(
            ErrorCode.HOMOGRAPHY_SINGULAR,
            "camera-to-plane mapping is singular",
            stage="geometry",
            field_path="extrinsic",
        ) from exc
    homography = panorama_intrinsic @ view_to_plane
    if not np.isfinite(homography).all():
        raise BusinessError(
            ErrorCode.HOMOGRAPHY_SINGULAR,
            "derived homography contains non-finite values",
            stage="geometry",
            field_path="extrinsic",
        )
    denominator = homography[2, 2]
    if abs(denominator) > 1e-15:
        homography = homography / denominator
    return qtuple(homography.reshape(-1))


def derive_scene_homographies(scene: SceneParameters) -> SceneParameters:
    from dataclasses import replace

    views = []
    for view in scene.views:
        homography = derive_homography(
            view.focal,
            view.principal_x,
            view.principal_y,
            view.extrinsic,
            scene.canvas_width,
            scene.canvas_height,
        )
        views.append(replace(view, homography=homography))
    return scene.with_views(views)


def check_homography(homography: tuple[float, ...]) -> np.ndarray:
    matrix = np.asarray(homography, dtype=np.float64).reshape(3, 3)
    if not np.isfinite(matrix).all():
        raise BusinessError(
            ErrorCode.HOMOGRAPHY_SINGULAR,
            "homography contains non-finite values",
            stage="geometry",
            field_path="homography",
        )
    determinant = float(np.linalg.det(matrix))
    if abs(determinant) < 1e-12:
        raise BusinessError(
            ErrorCode.HOMOGRAPHY_SINGULAR,
            "homography determinant is near zero",
            stage="geometry",
            field_path="homography",
            details={"determinant": determinant, "threshold": 1e-12},
        )
    try:
        return np.linalg.inv(matrix)
    except np.linalg.LinAlgError as exc:
        raise BusinessError(
            ErrorCode.HOMOGRAPHY_SINGULAR,
            "homography is singular",
            stage="geometry",
            field_path="homography",
            details={"determinant": determinant},
        ) from exc


def distort_normalized(
    x: np.ndarray, y: np.ndarray, chain: tuple[DistortionStage, ...]
) -> tuple[np.ndarray, np.ndarray]:
    """Apply a distortion chain in undistorted-to-distorted direction."""

    xd = np.asarray(x, dtype=np.float64).copy()
    yd = np.asarray(y, dtype=np.float64).copy()
    for stage in chain:
        r2 = xd * xd + yd * yd
        if stage.model == "radial":
            scale = 1.0 + stage.param("k1") * r2 + stage.param("k2") * r2 * r2
            xd = xd * scale
            yd = yd * scale
        elif stage.model == "scale":
            xd = xd * stage.param("sx", 1.0)
            yd = yd * stage.param("sy", 1.0)
        elif stage.model == "tangential":
            p1 = stage.param("p1")
            p2 = stage.param("p2")
            x_new = xd + 2.0 * p1 * xd * yd + p2 * (r2 + 2.0 * xd * xd)
            y_new = yd + p1 * (r2 + 2.0 * yd * yd) + 2.0 * p2 * xd * yd
            xd, yd = x_new, y_new
        else:
            raise BusinessError(
                ErrorCode.FORMAT_CORRUPT,
                f"unsupported distortion model {stage.model!r}",
                stage="parse",
                field_path="distortion_chain",
            )
    return xd, yd


def invert_radial_normalized(
    xd: np.ndarray,
    yd: np.ndarray,
    k1: float,
    k2: float,
    iterations: int = 10,
) -> tuple[np.ndarray, np.ndarray]:
    """Invert one radial stage with fixed-iteration Newton steps."""

    x = np.asarray(xd, dtype=np.float64).copy()
    y = np.asarray(yd, dtype=np.float64).copy()
    for _ in range(iterations):
        r2 = x * x + y * y
        scale = 1.0 + k1 * r2 + k2 * r2 * r2
        fx = x * scale - xd
        fy = y * scale - yd
        # Jacobian of p * (1 + k1 r^2 + k2 r^4).
        common = k1 + 2.0 * k2 * r2
        j00 = scale + 2.0 * x * x * common
        j01 = 2.0 * x * y * common
        j10 = j01
        j11 = scale + 2.0 * y * y * common
        determinant = j00 * j11 - j01 * j10
        safe = np.where(np.abs(determinant) < 1e-14, np.nan, determinant)
        dx = (fx * j11 - fy * j01) / safe
        dy = (fy * j00 - fx * j10) / safe
        dx = np.nan_to_num(dx, nan=0.0, posinf=0.0, neginf=0.0)
        dy = np.nan_to_num(dy, nan=0.0, posinf=0.0, neginf=0.0)
        x -= dx
        y -= dy
    return x, y


def fov_fingerprint(view: ViewParameters) -> str:
    from .model import sha256_json

    return sha256_json(view.semantic_dict())

