"""Deterministic descriptor matching, camera-aware inlier checks, and triangulation."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .canonical import canonical_float
from .errors import E_MATCH_INSUFFICIENT
from .io_views import Camera


def pair_id_for(first: int, second: int) -> str:
    low, high = min(first, second), max(first, second)
    return f"PAIR-V{low:04d}-V{high:04d}"


def pair_identifier(first: int, second: int) -> str:
    low, high = min(first, second), max(first, second)
    return f"{low:04d}-{high:04d}"


def camera_matrix(camera: Camera) -> np.ndarray:
    intrinsic = np.array(
        [
            [camera.fx, 0.0, camera.cx],
            [0.0, camera.fy, camera.cy],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    extrinsic = np.column_stack([camera.rotation, camera.translation])
    return intrinsic @ extrinsic


def camera_center(camera: Camera) -> np.ndarray:
    return -(camera.rotation.T @ camera.translation)


def project_point(camera: Camera, point: np.ndarray) -> tuple[np.ndarray, float]:
    camera_point = camera.rotation @ point + camera.translation
    depth = float(camera_point[2])
    if abs(depth) < 1.0e-12:
        raise ValueError("point projects to zero camera depth")
    pixel = np.array(
        [
            camera.fx * camera_point[0] / depth + camera.cx,
            camera.fy * camera_point[1] / depth + camera.cy,
        ],
        dtype=np.float64,
    )
    return pixel, depth


def _triangulate(first: Camera, second: Camera, pixel_a: np.ndarray, pixel_b: np.ndarray) -> np.ndarray:
    matrix_a = camera_matrix(first)
    matrix_b = camera_matrix(second)
    x_a, y_a = float(pixel_a[0]), float(pixel_a[1])
    x_b, y_b = float(pixel_b[0]), float(pixel_b[1])
    rows = np.vstack(
        [
            x_a * matrix_a[2] - matrix_a[0],
            y_a * matrix_a[2] - matrix_a[1],
            x_b * matrix_b[2] - matrix_b[0],
            y_b * matrix_b[2] - matrix_b[1],
        ]
    )
    _, _, right_singular = np.linalg.svd(rows, full_matrices=False)
    homogeneous = right_singular[-1]
    if abs(float(homogeneous[3])) < 1.0e-12:
        raise ValueError("point at infinity")
    point = homogeneous[:3] / homogeneous[3]
    if not np.isfinite(point).all():
        raise ValueError("non-finite triangulation")
    return point


def _parallax_degrees(point: np.ndarray, first: Camera, second: Camera) -> float:
    center_a = camera_center(first)
    center_b = camera_center(second)
    ray_a = point - center_a
    ray_b = point - center_b
    norm_a = float(np.linalg.norm(ray_a))
    norm_b = float(np.linalg.norm(ray_b))
    if norm_a < 1.0e-12 or norm_b < 1.0e-12:
        return 0.0
    cosine = float(np.clip(np.dot(ray_a, ray_b) / (norm_a * norm_b), -1.0, 1.0))
    return math.degrees(math.acos(cosine))


def _descriptor_matches(
    features_a: list[dict[str, Any]],
    features_b: list[dict[str, Any]],
    matching_params: dict[str, Any],
) -> list[tuple[int, int, float]]:
    if not features_a or not features_b:
        return []
    descriptors_a = np.asarray([item["descriptor"] for item in features_a], dtype=np.float64)
    descriptors_b = np.asarray([item["descriptor"] for item in features_b], dtype=np.float64)
    if not np.isfinite(descriptors_a).all() or not np.isfinite(descriptors_b).all():
        raise ValueError("non-finite descriptor reached the matching stage")
    distances = np.sqrt(
        np.maximum(
            0.0,
            np.sum((descriptors_a[:, None, :] - descriptors_b[None, :, :]) ** 2, axis=2),
        )
    )
    nearest_a = np.argmin(distances, axis=1)
    nearest_b = np.argmin(distances, axis=0)
    maximum_distance = float(matching_params["descriptor_max_distance"])
    ratio = float(matching_params["ratio_test"])
    matches: list[tuple[int, int, float]] = []
    for index_a, index_b in enumerate(nearest_a.tolist()):
        if int(nearest_b[index_b]) != index_a:
            continue
        best = float(distances[index_a, index_b])
        if best > maximum_distance:
            continue
        if distances.shape[1] > 1:
            row = distances[index_a].copy()
            row[index_b] = np.inf
            second = float(np.min(row))
            if not math.isfinite(second) or best > ratio * second:
                continue
        matches.append((index_a, int(index_b), best))
    matches.sort(key=lambda item: (item[0], item[1], item[2]))
    return matches


def match_and_triangulate_pair(
    *,
    feature_a: dict[str, Any],
    feature_b: dict[str, Any],
    camera_a: Camera,
    camera_b: Camera,
    matching_params: dict[str, Any],
    triangulation_params: dict[str, Any],
    feature_key_a: str,
    feature_key_b: str,
) -> dict[str, Any]:
    # Canonical ordering makes the same pair bit-for-bit identical even when
    # callers pass the two views in the opposite order.
    if camera_a.view_index > camera_b.view_index:
        feature_a, feature_b = feature_b, feature_a
        camera_a, camera_b = camera_b, camera_a
        feature_key_a, feature_key_b = feature_key_b, feature_key_a
    if camera_a.view_index == camera_b.view_index:
        raise ValueError("a match pair requires two distinct views")
    pair_id = pair_id_for(camera_a.view_index, camera_b.view_index)
    features_a = feature_a["features"]
    features_b = feature_b["features"]
    descriptor_matches = _descriptor_matches(features_a, features_b, matching_params)

    minimum_parallax = float(triangulation_params["min_parallax_deg"])
    maximum_error = float(triangulation_params["max_reprojection_error_px"])
    minimum_depth = float(triangulation_params["min_depth"])
    maximum_depth = float(triangulation_params["max_depth"])
    matches: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    inlier_count = 0
    for ordinal, (index_a, index_b, distance) in enumerate(descriptor_matches, 1):
        match_id = f"MATCH-{pair_id[5:]}-{ordinal:06d}"
        first_feature = features_a[index_a]
        second_feature = features_b[index_b]
        pixel_a = np.asarray(first_feature["pixel"], dtype=np.float64)
        pixel_b = np.asarray(second_feature["pixel"], dtype=np.float64)
        inlier = False
        reason = ""
        point: np.ndarray | None = None
        reprojection_error = math.inf
        parallax = 0.0
        try:
            point = _triangulate(camera_a, camera_b, pixel_a, pixel_b)
            projected_a, depth_a = project_point(camera_a, point)
            projected_b, depth_b = project_point(camera_b, point)
            if depth_a <= 0.0 or depth_b <= 0.0:
                reason = "point_not_in_front_of_both_cameras"
            elif not (minimum_depth <= depth_a <= maximum_depth and minimum_depth <= depth_b <= maximum_depth):
                reason = "depth_out_of_range"
            else:
                error_a = float(np.linalg.norm(projected_a - pixel_a))
                error_b = float(np.linalg.norm(projected_b - pixel_b))
                reprojection_error = max(error_a, error_b)
                parallax = _parallax_degrees(point, camera_a, camera_b)
                if parallax + 1.0e-12 < minimum_parallax:
                    reason = "parallax_below_minimum"
                elif reprojection_error > maximum_error:
                    reason = "reprojection_error_above_maximum"
                else:
                    inlier = True
        except Exception as error:
            reason = "triangulation_failed:" + type(error).__name__

        match_record = {
            "match_id": match_id,
            "feature_ids": [
                str(first_feature["feature_id"]),
                str(second_feature["feature_id"]),
            ],
            "pixels": [
                [int(pixel_a[0]), int(pixel_a[1])],
                [int(pixel_b[0]), int(pixel_b[1])],
            ],
            "descriptor_distance": canonical_float(distance, 10),
            "inlier": inlier,
            "reprojection_error_px": (
                canonical_float(reprojection_error, 10) if math.isfinite(reprojection_error) else None
            ),
            "parallax_deg": canonical_float(parallax, 10),
        }
        if reason:
            match_record["reason"] = reason
        matches.append(match_record)
        if inlier and point is not None:
            inlier_count += 1
            point_record = {
                "point_id": f"PT-{pair_id[5:]}-{inlier_count:06d}",
                "match_id": match_id,
                "xyz": [canonical_float(value, 10) for value in point.tolist()],
                "source_views": [camera_a.view_index, camera_b.view_index],
                "reprojection_error_px": canonical_float(reprojection_error, 10),
                "parallax_deg": canonical_float(parallax, 10),
            }
            points.append(point_record)

    minimum_inliers = int(matching_params["min_inliers"])
    insufficient = inlier_count < minimum_inliers
    result: dict[str, Any] = {
        "pair_id": pair_id,
        "view_indices": [camera_a.view_index, camera_b.view_index],
        "feature_cache_keys": [feature_key_a, feature_key_b],
        "descriptor_match_count": len(matches),
        "inlier_count": inlier_count,
        "minimum_inliers": minimum_inliers,
        "status": "insufficient" if insufficient else "ok",
        "business_code": E_MATCH_INSUFFICIENT if insufficient else None,
        "matches": matches,
        "points": [] if insufficient else points,
    }
    if insufficient:
        result["failure_detail"] = {
            "reason": "inlier_count_below_minimum",
            "descriptor_match_count": len(matches),
            "inlier_count": inlier_count,
            "minimum_inliers": minimum_inliers,
        }
    return result
