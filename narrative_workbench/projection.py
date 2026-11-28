from __future__ import annotations

import math
from typing import Any

import numpy as np

from .models import CameraFrame, GaussianElement


def camera_basis(camera: CameraFrame) -> tuple[np.ndarray, np.ndarray]:
    eye = np.asarray(camera.eye, dtype=np.float64)
    target = np.asarray(camera.target, dtype=np.float64)
    up_world = np.asarray(camera.up, dtype=np.float64)
    forward = target - eye
    forward_norm = float(np.linalg.norm(forward))
    if forward_norm <= 1.0e-12:
        raise ValueError("camera eye and target coincide")
    forward /= forward_norm
    right = np.cross(forward, up_world)
    right_norm = float(np.linalg.norm(right))
    if right_norm <= 1.0e-12:
        raise ValueError("camera up vector is parallel to view direction")
    right /= right_norm
    up = np.cross(right, forward)
    up /= np.linalg.norm(up)
    rotation = np.vstack((right, up, forward))
    translation = -rotation @ eye
    return rotation, translation


def view_point(rotation: np.ndarray, translation: np.ndarray, point: np.ndarray) -> np.ndarray:
    return rotation @ np.asarray(point, dtype=np.float64) + translation


def projection_terms(camera: CameraFrame) -> tuple[float, float]:
    tan_y = math.tan(math.radians(camera.fov_y_deg) * 0.5)
    tan_x = tan_y * float(camera.width) / float(camera.height)
    return tan_x, tan_y


def _canonical_axis(vector: np.ndarray) -> np.ndarray:
    result = np.asarray(vector, dtype=np.float64).copy()
    if result[0] < -1.0e-14 or (abs(result[0]) <= 1.0e-14 and result[1] < 0.0):
        result *= -1.0
    return result


def _ellipse_from_covariance(covariance: np.ndarray) -> dict[str, Any]:
    c = 0.5 * (np.asarray(covariance, dtype=np.float64) + np.asarray(covariance, dtype=np.float64).T)
    eigenvalues, eigenvectors = np.linalg.eigh(c)
    order = np.argsort(eigenvalues)[::-1]
    eigenvalues = np.maximum(eigenvalues[order], 0.0)
    axes = eigenvectors[:, order]
    major_axis = _canonical_axis(axes[:, 0])
    minor_axis = np.array([-major_axis[1], major_axis[0]], dtype=np.float64)
    semi_major = math.sqrt(float(eigenvalues[0]))
    semi_minor = math.sqrt(float(eigenvalues[1]))
    orientation = math.degrees(math.atan2(float(major_axis[1]), float(major_axis[0])))
    if orientation < 0.0:
        orientation += 180.0
    return {
        "screen_covariance": [
            float(c[0, 0]),
            float(c[0, 1]),
            float(c[1, 1]),
        ],
        "eigenvalues": [float(eigenvalues[0]), float(eigenvalues[1])],
        "semi_axis_major": float(semi_major),
        "semi_axis_minor": float(semi_minor),
        "major_axis_length": float(2.0 * semi_major),
        "minor_axis_length": float(2.0 * semi_minor),
        "major_axis_vector": [float(v) for v in major_axis],
        "minor_axis_vector": [float(v) for v in minor_axis],
        "orientation_deg": float(orientation),
        "axis_convention": "semi-axis=sqrt(eigenvalue), angle=CCW from +x in y-down pixel space, range [0,180)",
    }


def project_element(element: GaussianElement, camera: CameraFrame) -> dict[str, Any]:
    rotation, translation = camera_basis(camera)
    tan_x, tan_y = projection_terms(camera)
    center_view = view_point(rotation, translation, element.center)
    x, y, z = (float(v) for v in center_view)
    covariance_view = rotation @ element.covariance @ rotation.T
    depth_variance = float(rotation[2] @ element.covariance @ rotation[2].T)
    depth_sigma = math.sqrt(max(depth_variance, 0.0))
    result: dict[str, Any] = {
        "id": element.element_id,
        "layer_index": int(element.layer_index),
        "layer_name": element.layer_name,
        "world_center": [float(v) for v in element.center],
        "view_center": [x, y, z],
        "centroid_depth": z,
        "depth_sigma": depth_sigma,
        "covariance_encoding": "xx,xy,xz,yy,yz,zz",
        "world_covariance": [
            float(element.covariance[0, 0]),
            float(element.covariance[0, 1]),
            float(element.covariance[0, 2]),
            float(element.covariance[1, 1]),
            float(element.covariance[1, 2]),
            float(element.covariance[2, 2]),
        ],
        "plane_normal": [float(v) for v in element.plane_normal],
        "status": "visible",
        "partially_clipped": False,
        "clip_edges": [],
        "cull_reason": None,
    }
    if z <= camera.near:
        result.update(
            status="culled",
            cull_reason="near_plane_center",
            clipping_basis=f"centroid depth {z:.9g} <= near {camera.near:.9g}",
        )
        return result
    if z + 3.0 * depth_sigma >= camera.far:
        result.update(
            status="culled",
            cull_reason="far_plane_extent",
            clipping_basis=f"centroid {z:.9g} + 3*sigma_z {3.0 * depth_sigma:.9g} >= far {camera.far:.9g}",
        )
        return result
    if z - 3.0 * depth_sigma <= camera.near:
        result.update(
            status="culled",
            cull_reason="near_plane_extent",
            clipping_basis=f"centroid {z:.9g} - 3*sigma_z {3.0 * depth_sigma:.9g} <= near {camera.near:.9g}",
        )
        return result

    center_x = 0.5 * camera.width + camera.width * x / (2.0 * tan_x * z)
    center_y = 0.5 * camera.height - camera.height * y / (2.0 * tan_y * z)
    jacobian = np.array(
        [
            [
                camera.width / (2.0 * tan_x * z),
                0.0,
                -camera.width * x / (2.0 * tan_x * z * z),
            ],
            [
                0.0,
                -camera.height / (2.0 * tan_y * z),
                camera.height * y / (2.0 * tan_y * z * z),
            ],
        ],
        dtype=np.float64,
    )
    screen_covariance = jacobian @ covariance_view @ jacobian.T
    screen_covariance = 0.5 * (screen_covariance + screen_covariance.T)
    ellipse = _ellipse_from_covariance(screen_covariance)
    semi_major = float(ellipse["semi_axis_major"])
    semi_minor = float(ellipse["semi_axis_minor"])
    support = 3.0 * max(semi_major, semi_minor)
    x0, x1 = center_x - support, center_x + support
    y0, y1 = center_y - support, center_y + support

    result.update(ellipse)
    result.update(
        {
            "screen_center": [float(center_x), float(center_y)],
            "support_bbox_3sigma": [float(x0), float(y0), float(x1), float(y1)],
        }
    )

    if x1 < 0.5 or x0 > camera.width - 0.5 or y1 < 0.5 or y0 > camera.height - 0.5:
        result.update(
            status="culled",
            cull_reason="outside_viewport",
            clipping_basis=(
                f"3-sigma screen bbox [{x0:.6g},{y0:.6g},{x1:.6g},{y1:.6g}] "
                f"does not intersect pixel-center viewport [0.5,{camera.width - 0.5}]x[0.5,{camera.height - 0.5}]"
            ),
        )
        return result

    min_x = max(0, int(math.ceil(x0 - 0.5)))
    max_x = min(camera.width - 1, int(math.floor(x1 - 0.5)))
    min_y = max(0, int(math.ceil(y0 - 0.5)))
    max_y = min(camera.height - 1, int(math.floor(y1 - 0.5)))
    if min_x > max_x or min_y > max_y:
        result.update(
            status="culled",
            cull_reason="outside_viewport_pixel_centers",
            clipping_basis="3-sigma bbox intersects no pixel center",
        )
        return result

    clip_edges: list[str] = []
    if x0 < 0.5:
        clip_edges.append("left")
    if x1 > camera.width - 0.5:
        clip_edges.append("right")
    if y0 < 0.5:
        clip_edges.append("top")
    if y1 > camera.height - 0.5:
        clip_edges.append("bottom")
    result.update(
        {
            "render_bbox": [int(min_x), int(min_y), int(max_x), int(max_y)],
            "partially_clipped": bool(clip_edges),
            "clip_edges": clip_edges,
            "clipping_basis": (
                f"viewport-intersected 3-sigma bbox; clipped edges: {','.join(clip_edges)}"
                if clip_edges
                else "3-sigma bbox fully inside viewport"
            ),
        }
    )
    return result


def project_frame(
    elements: list[GaussianElement],
    camera: CameraFrame,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    projected: list[dict[str, Any]] = []
    culled: list[dict[str, Any]] = []
    for element in elements:
        record = project_element(element, camera)
        if record["status"] == "visible":
            projected.append(record)
        else:
            culled.append(record)
    return projected, culled


def pixel_depth_map(
    element: GaussianElement,
    camera: CameraFrame,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> np.ndarray:
    """Ray/central-plane intersection depth for pixels in an inclusive bbox.

    The plane passes through the Gaussian centroid and is normal to its thinnest
    principal axis. A fallback to centroid depth is explicit in the validity mask.
    """
    rotation, translation = camera_basis(camera)
    tan_x, tan_y = projection_terms(camera)
    center_view = view_point(rotation, translation, element.center)
    normal_view = rotation @ element.plane_normal
    xs = np.arange(x0, x1 + 1, dtype=np.float64) + 0.5
    ys = np.arange(y0, y1 + 1, dtype=np.float64) + 0.5
    sx = xs[None, :]
    sy = ys[:, None]
    dx = (sx - 0.5 * camera.width) * (2.0 * tan_x / camera.width)
    dy = -(sy - 0.5 * camera.height) * (2.0 * tan_y / camera.height)
    denominator = normal_view[0] * dx + normal_view[1] * dy + normal_view[2]
    numerator = float(normal_view @ center_view)
    safe = np.abs(denominator) > 1.0e-12
    depth = np.full_like(dx + dy, float(center_view[2]), dtype=np.float64)
    depth[safe] = numerator / denominator[safe]
    valid = safe & (depth > camera.near) & (depth < camera.far)
    depth = np.where(valid, depth, float(center_view[2]))
    return depth
