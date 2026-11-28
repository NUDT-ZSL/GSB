from __future__ import annotations

import itertools
import math
from typing import Any

import numpy as np

from .generator import GeneratedProject
from .schemas import load_camera_frames
from .storage import array_sha256, read_json, sha256_file, write_json_atomic


def _view_basis(camera: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    eye = np.asarray(camera["eye"], dtype=np.float64)
    target = np.asarray(camera["target"], dtype=np.float64)
    up_world = np.asarray(camera["up"], dtype=np.float64)
    forward = target - eye
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, up_world)
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    rotation = np.vstack([right, up, forward])
    return rotation, -rotation @ eye


def _project_view_point(
    view: np.ndarray, camera: dict[str, Any]
) -> np.ndarray:
    width = int(camera["width"])
    height = int(camera["height"])
    tan_y = math.tan(math.radians(float(camera["fov_y_deg"])) * 0.5)
    tan_x = tan_y * width / height
    return np.array(
        [
            0.5 * width + width * float(view[0]) / (2.0 * tan_x * float(view[2])),
            0.5 * height
            - height * float(view[1]) / (2.0 * tan_y * float(view[2])),
        ],
        dtype=np.float64,
    )


def _fd_projection(
    center: np.ndarray,
    covariance: np.ndarray,
    camera: dict[str, Any],
) -> dict[str, Any] | None:
    rotation, translation = _view_basis(camera)
    center_view = rotation @ center + translation
    z = float(center_view[2])
    near = float(camera["near"])
    far = float(camera["far"])
    c_view = rotation @ covariance @ rotation.T
    depth_sigma = math.sqrt(max(float(rotation[2] @ covariance @ rotation[2].T), 0.0))
    if z <= near or z + 3.0 * depth_sigma >= far or z - 3.0 * depth_sigma <= near:
        return None
    center_screen = _project_view_point(center_view, camera)
    jacobian = np.empty((2, 3), dtype=np.float64)
    for axis in range(3):
        step = 1.0e-5 * max(1.0, abs(float(center_view[axis])))
        plus = center_view.copy()
        minus = center_view.copy()
        plus[axis] += step
        minus[axis] -= step
        jacobian[:, axis] = (
            _project_view_point(plus, camera) - _project_view_point(minus, camera)
        ) / (2.0 * step)
    screen_covariance = jacobian @ c_view @ jacobian.T
    eigenvalues, eigenvectors = np.linalg.eigh(
        0.5 * (screen_covariance + screen_covariance.T)
    )
    order = np.argsort(eigenvalues)[::-1]
    eigenvalues = np.maximum(eigenvalues[order], 0.0)
    axis = eigenvectors[:, order[0]]
    if axis[0] < 0.0 or (abs(axis[0]) <= 1.0e-14 and axis[1] < 0.0):
        axis = -axis
    orientation = math.degrees(math.atan2(float(axis[1]), float(axis[0])))
    if orientation < 0.0:
        orientation += 180.0
    support = 3.0 * max(math.sqrt(float(eigenvalues[0])), math.sqrt(float(eigenvalues[1])))
    x0 = float(center_screen[0]) - support
    x1 = float(center_screen[0]) + support
    y0 = float(center_screen[1]) - support
    y1 = float(center_screen[1]) + support
    width = int(camera["width"])
    height = int(camera["height"])
    if x1 < 0.5 or x0 > width - 0.5 or y1 < 0.5 or y0 > height - 0.5:
        return None
    min_x = max(0, math.ceil(x0 - 0.5))
    max_x = min(width - 1, math.floor(x1 - 0.5))
    min_y = max(0, math.ceil(y0 - 0.5))
    max_y = min(height - 1, math.floor(y1 - 0.5))
    if min_x > max_x or min_y > max_y:
        return None
    return {
        "screen_center": center_screen,
        "screen_covariance": screen_covariance,
        "semi_major": math.sqrt(float(eigenvalues[0])),
        "semi_minor": math.sqrt(float(eigenvalues[1])),
        "orientation": orientation,
        "centroid_depth": z,
        "bbox": [min_x, min_y, max_x, max_y],
    }


def _curve_gain(points: list[list[float]], value: float) -> float:
    x = float(np.clip(value, 0.0, 1.0))
    if x <= points[0][0]:
        return float(np.clip(points[0][1], 0.0, 1.0))
    if x >= points[-1][0]:
        return float(np.clip(points[-1][1], 0.0, 1.0))
    for index in range(1, len(points)):
        x0, y0 = points[index - 1]
        x1, y1 = points[index]
        if x <= x1:
            return float(np.clip(y0 + (y1 - y0) * (x - x0) / (x1 - x0), 0.0, 1.0))
    return 0.0


def _plane_depths(
    element: dict[str, Any],
    projection: dict[str, Any],
    camera: dict[str, Any],
) -> np.ndarray:
    rotation, translation = _view_basis(camera)
    center_view = rotation @ np.asarray(element["center"], dtype=np.float64) + translation
    normal_view = rotation @ np.asarray(element["plane_normal"], dtype=np.float64)
    tan_y = math.tan(math.radians(float(camera["fov_y_deg"])) * 0.5)
    tan_x = tan_y * int(camera["width"]) / int(camera["height"])
    x0, y0, x1, y1 = projection["bbox"]
    xs = np.arange(x0, x1 + 1, dtype=np.float64) + 0.5
    ys = np.arange(y0, y1 + 1, dtype=np.float64) + 0.5
    dx = (xs[None, :] - 0.5 * int(camera["width"])) * (
        2.0 * tan_x / int(camera["width"])
    )
    dy = -(ys[:, None] - 0.5 * int(camera["height"])) * (
        2.0 * tan_y / int(camera["height"])
    )
    denominator = normal_view[0] * dx + normal_view[1] * dy + normal_view[2]
    numerator = float(normal_view @ center_view)
    valid = np.abs(denominator) > 1.0e-12
    result = np.full_like(dx + dy, float(center_view[2]), dtype=np.float64)
    result[valid] = numerator / denominator[valid]
    valid &= (result > float(camera["near"])) & (result < float(camera["far"]))
    return np.where(valid, result, float(center_view[2]))


def _build_reference_stack(
    elements: list[dict[str, Any]],
    camera: dict[str, Any],
) -> tuple[list[dict[str, Any]], np.ndarray, np.ndarray, np.ndarray]:
    height = int(camera["height"])
    width = int(camera["width"])
    visible: list[dict[str, Any]] = []
    for element in elements:
        projection = _fd_projection(
            np.asarray(element["center"], dtype=np.float64),
            np.asarray(
                [
                    [element["covariance"][0], element["covariance"][1], element["covariance"][2]],
                    [element["covariance"][1], element["covariance"][3], element["covariance"][4]],
                    [element["covariance"][2], element["covariance"][4], element["covariance"][5]],
                ],
                dtype=np.float64,
            ),
            camera,
        )
        if projection is None:
            continue
        visible.append({"element": element, "projection": projection})
    count = len(visible)
    alphas = np.zeros((count, height, width), dtype=np.float64)
    depths = np.empty((count, height, width), dtype=np.float64)
    colors = np.zeros((count, 3), dtype=np.float64)
    for index, item in enumerate(visible):
        element = item["element"]
        projection = item["projection"]
        colors[index] = element["color"]
        x0, y0, x1, y1 = projection["bbox"]
        depths[index, y0 : y1 + 1, x0 : x1 + 1] = _plane_depths(
            element, projection, camera
        )
        covariance = projection["screen_covariance"]
        inverse = np.linalg.inv(
            0.5 * (covariance + covariance.T) + np.eye(2) * 1.0e-14
        )
        xs = np.arange(x0, x1 + 1, dtype=np.float64) + 0.5
        ys = np.arange(y0, y1 + 1, dtype=np.float64) + 0.5
        dx = xs[None, :] - projection["screen_center"][0]
        dy = ys[:, None] - projection["screen_center"][1]
        q = inverse[0, 0] * dx * dx + 2.0 * inverse[0, 1] * dx * dy + inverse[1, 1] * dy * dy
        normalized_depth = float(
            np.clip(
                (projection["centroid_depth"] - float(camera["near"]))
                / (float(camera["far"]) - float(camera["near"])),
                0.0,
                1.0,
            )
        )
        gain = _curve_gain(element["depth_curve"]["control_points"], normalized_depth)
        actual_opacity = float(np.clip(element["opacity"] * gain, 0.0, 1.0))
        alpha = np.where(q <= 9.0, actual_opacity * np.exp(-0.5 * q), 0.0)
        alpha = np.where(alpha >= 1.0e-5, alpha, 0.0)
        alphas[index, y0 : y1 + 1, x0 : x1 + 1] = alpha
        projection["actual_opacity"] = actual_opacity
    return visible, alphas, depths, colors


def _composite_order(
    order: list[int],
    alphas: np.ndarray,
    colors: np.ndarray,
) -> np.ndarray:
    result = np.zeros((*alphas.shape[1:], 4), dtype=np.float64)
    for index in order:
        alpha = alphas[index]
        previous_alpha = result[..., 3]
        next_alpha = alpha + previous_alpha * (1.0 - alpha)
        numerator = (
            colors[index] * alpha[..., None]
            + result[..., :3] * previous_alpha[..., None] * (1.0 - alpha[..., None])
        )
        result[..., :3] = np.where(
            next_alpha[..., None] > 1.0e-15,
            numerator / np.maximum(next_alpha[..., None], 1.0e-15),
            0.0,
        )
        result[..., 3] = next_alpha
    return result


def _pixel_sorted(
    indices: list[int],
    alphas: np.ndarray,
    depths: np.ndarray,
    colors: np.ndarray,
) -> np.ndarray:
    result = np.zeros((*alphas.shape[1:], 4), dtype=np.float64)
    if not indices:
        return result
    stack_alpha = alphas[indices]
    stack_depth = depths[indices]
    key = np.where(stack_alpha >= 1.0e-5, stack_depth, -np.inf)
    order = np.argsort(-key, axis=0, kind="stable")
    for rank in range(len(indices)):
        selected = order[rank]
        alpha = np.take_along_axis(stack_alpha, selected[None], axis=0)[0]
        color = colors[indices][selected]
        previous_alpha = result[..., 3]
        next_alpha = alpha + previous_alpha * (1.0 - alpha)
        numerator = (
            color * alpha[..., None]
            + result[..., :3] * previous_alpha[..., None] * (1.0 - alpha[..., None])
        )
        result[..., :3] = np.where(
            next_alpha[..., None] > 1.0e-15,
            numerator / np.maximum(next_alpha[..., None], 1.0e-15),
            0.0,
        )
        result[..., 3] = next_alpha
    return result


def _independent_violation_mask(
    visible: list[dict[str, Any]],
    draw_order: list[int],
    alphas: np.ndarray,
    depths: np.ndarray,
    shape: tuple[int, int],
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    rank = {index: position for position, index in enumerate(draw_order)}
    mask = np.zeros(shape, dtype=bool)
    details: list[dict[str, Any]] = []
    for first, second in itertools.combinations(range(len(visible)), 2):
        overlap = (alphas[first] >= 0.012) & (alphas[second] >= 0.012)
        if not np.any(overlap):
            continue
        first_before = rank[first] < rank[second]
        reversal = overlap & ((depths[first] > depths[second]) != first_before)
        count = int(np.count_nonzero(reversal))
        if count == 0:
            continue
        mask |= reversal
        coords = np.argwhere(reversal)
        details.append(
            {
                "pair": [
                    visible[first]["element"]["id"],
                    visible[second]["element"]["id"],
                ],
                "count": count,
                "first_pixel": [int(coords[0, 1]), int(coords[0, 0])],
            }
        )
    return mask, details


def _compare_projection_records(
    independent: list[dict[str, Any]],
    stored: list[dict[str, Any]],
) -> dict[str, Any]:
    expected = {record["id"]: record for record in stored}
    actual = {item["element"]["id"]: item for item in independent}
    center_error = 0.0
    covariance_error = 0.0
    axis_error = 0.0
    orientation_error = 0.0
    for element_id in sorted(set(expected).intersection(actual)):
        first = expected[element_id]
        second = actual[element_id]
        center_error = max(
            center_error,
            float(
                np.linalg.norm(
                    np.asarray(first["screen_center"])
                    - np.asarray(second["projection"]["screen_center"])
                )
            ),
        )
        covariance_error = max(
            covariance_error,
            float(
                np.max(
                    np.abs(
                        np.asarray(first["screen_covariance"])
                        - np.asarray(
                            [
                                second["projection"]["screen_covariance"][0, 0],
                                second["projection"]["screen_covariance"][0, 1],
                                second["projection"]["screen_covariance"][1, 1],
                            ]
                        )
                    )
                )
            ),
        )
        axis_error = max(
            axis_error,
            abs(float(first["semi_axis_major"]) - second["projection"]["semi_major"]),
            abs(float(first["semi_axis_minor"]) - second["projection"]["semi_minor"]),
        )
        difference = abs(
            float(first["orientation_deg"]) - second["projection"]["orientation"]
        )
        orientation_error = max(orientation_error, min(difference, 180.0 - difference))
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    passed = (
        not missing
        and not extra
        and center_error <= 1.0e-5
        and covariance_error <= 1.0e-5
        and axis_error <= 1.0e-5
        and orientation_error <= 1.0e-4
    )
    return {
        "passed": bool(passed),
        "missing": missing,
        "extra": extra,
        "max_center_error": center_error,
        "max_covariance_error": covariance_error,
        "max_axis_error": axis_error,
        "max_orientation_error_deg": orientation_error,
    }


def verify_frames(project: GeneratedProject) -> dict[str, Any]:
    camera_frames = load_camera_frames(project.camera_file)
    normalized_json = {
        version: read_json(project.root / "normalized" / f"{version}.json")
        for version in ("v1", "v2", "v3")
    }
    camera_json = read_json(project.camera_file)
    reference_json = read_json(project.reference_file)
    frame_results: list[dict[str, Any]] = []
    failures: list[str] = []
    checkpoint_integrity: dict[str, Any] = {}
    for version in ("v1", "v2", "v3"):
        checkpoint = read_json(project.root / "frames" / version / "checkpoint.json")
        hash_mismatches: list[str] = []
        for committed in checkpoint["committed_frames"]:
            for field, filename in (
                ("json_sha256", committed["json"]),
                ("data_sha256", committed["data"]),
                ("png_sha256", committed["png"]),
            ):
                actual = sha256_file(project.root / "frames" / version / filename)
                if actual != committed[field]:
                    hash_mismatches.append(
                        f"frame_{int(committed['index']):04d}:{filename}"
                    )
        checkpoint_integrity[version] = {
            "passed": not hash_mismatches and int(checkpoint["next_frame"]) == int(
                checkpoint["total_frames"]
            ),
            "hash_mismatches": hash_mismatches,
            "next_frame": int(checkpoint["next_frame"]),
            "total_frames": int(checkpoint["total_frames"]),
        }
        if not checkpoint_integrity[version]["passed"]:
            failures.append(f"{version}:checkpoint_integrity")
        element_by_id = {
            element["id"]: element for element in normalized_json[version]["elements"]
        }
        stable_by_id = {
            element["id"]: int(element["stable_index"])
            for element in normalized_json[version]["elements"]
        }
        for committed in checkpoint["committed_frames"]:
            frame_index = int(committed["index"])
            frame_json = read_json(
                project.root / "frames" / version / committed["json"]
            )
            arrays = np.load(
                project.root / "frames" / version / committed["data"],
                allow_pickle=False,
            )
            camera = camera_json["frames"][frame_index]
            visible, alphas, depths, colors = _build_reference_stack(
                normalized_json[version]["elements"], camera
            )
            visible_by_id = {
                item["element"]["id"]: item for item in visible
            }
            projection_check = _compare_projection_records(
                visible, frame_json["projection"]["visible"]
            )
            reference_projection_check = _compare_projection_records(
                visible, reference_json["frames"][frame_index]["elements"]
            )
            stack_order = sorted(
                range(len(visible)),
                key=lambda index: (
                    -visible[index]["projection"]["centroid_depth"],
                    int(visible[index]["element"]["layer_index"]),
                    stable_by_id[visible[index]["element"]["id"]],
                    visible[index]["element"]["id"],
                ),
            )
            order_ids = [visible[index]["element"]["id"] for index in stack_order]
            stored_order_ids = [record["id"] for record in frame_json["draw_order"]]
            order_match = order_ids == stored_order_ids
            independent_rgba = _composite_order(stack_order, alphas, colors)
            rgba_error = float(np.max(np.abs(independent_rgba - arrays["rgba"])))
            independent_reference = _pixel_sorted(
                list(range(len(visible))), alphas, depths, colors
            )
            pixel_reference_error = float(
                np.max(np.abs(independent_reference - arrays["pixel_reference_rgba"]))
            )
            violation_mask, violation_details = _independent_violation_mask(
                visible,
                stack_order,
                alphas,
                depths,
                (int(camera["height"]), int(camera["width"])),
            )
            stored_mask = arrays["sorting_violation_mask"].astype(bool)
            mask_mismatch = int(np.count_nonzero(violation_mask != stored_mask))
            stored_pairs = {
                tuple(sorted([item["element_a"], item["element_b"]]))
                for item in frame_json["sorting"]["violations"]
            }
            independent_pairs = {
                tuple(sorted(item["pair"])) for item in violation_details
            }
            pair_match = stored_pairs == independent_pairs
            culled_reference = set(element_by_id) - set(visible_by_id)
            stored_culled = {
                item["id"] for item in frame_json["projection"]["culled"]
            }
            culled_match = culled_reference == stored_culled
            reference_culled = {
                item["id"] for item in reference_json["frames"][frame_index]["culled"]
            }
            reference_culled_match = culled_reference == reference_culled
            passed = (
                projection_check["passed"]
                and reference_projection_check["passed"]
                and order_match
                and rgba_error <= 2.0e-5
                and pixel_reference_error <= 2.0e-5
                and mask_mismatch <= 2
                and pair_match
                and culled_match
                and reference_culled_match
                and committed["rgba_sha256"] == array_sha256(arrays["rgba"])
            )
            record = {
                "version": version,
                "frame_index": frame_index,
                "passed": bool(passed),
                "projection": projection_check,
                "reference_ellipse_projection": reference_projection_check,
                "draw_order_match": bool(order_match),
                "independent_draw_order": order_ids,
                "stored_draw_order": stored_order_ids,
                "max_rgba_difference_from_independent_composite": rgba_error,
                "max_pixel_reference_difference": pixel_reference_error,
                "sorting_violation_mask_mismatch_pixels": mask_mismatch,
                "sorting_violation_pairs_match": bool(pair_match),
                "culled_set_match": bool(culled_match),
                "reference_culled_set_match": bool(reference_culled_match),
                "rgba_hash_match": bool(
                    committed["rgba_sha256"] == array_sha256(arrays["rgba"])
                ),
                "independent_violation_pairs": sorted(
                    [list(item) for item in independent_pairs]
                ),
                "stored_violation_pairs": sorted([list(item) for item in stored_pairs]),
            }
            frame_results.append(record)
            if not passed:
                failures.append(f"{version}:frame_{frame_index:04d}")
    result = {
        "schema_version": "independent-verification/1",
        "scene_fingerprint": project.fingerprint,
        "method": {
            "projection": "central finite-difference Jacobian from raw normalized covariance",
            "ordering": "independent centroid-depth stable sort",
            "composite": "independent far-to-near alpha-over implementation",
            "pixel_reference": "independent per-pixel central-plane depth sort",
            "violation": "independent pair overlap reversal scan at alpha >= 0.012",
        },
        "passed": not failures,
        "frame_count": len(frame_results),
        "failure_count": len(failures),
        "failures": failures,
        "checkpoint_integrity": checkpoint_integrity,
        "frames": frame_results,
    }
    write_json_atomic(project.root / "verification.json", result)
    return result
