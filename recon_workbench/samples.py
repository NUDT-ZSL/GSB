"""Deterministic synthetic multi-view samples used by the workbench."""

from __future__ import annotations

import colorsys
import json
import math
import shutil
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from .canonical import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_float,
    sha256_bytes,
    sha256_json,
)
from .injection import InjectionSet


def _look_at(camera_center: np.ndarray, target: np.ndarray) -> np.ndarray:
    forward = target - camera_center
    forward /= np.linalg.norm(forward)
    world_up = np.array([0.0, 0.0, 1.0], dtype=np.float64)
    x_axis = np.cross(forward, world_up)
    norm = np.linalg.norm(x_axis)
    if norm < 1.0e-9:
        world_up = np.array([0.0, 1.0, 0.0], dtype=np.float64)
        x_axis = np.cross(forward, world_up)
        norm = np.linalg.norm(x_axis)
    x_axis /= norm
    y_axis = np.cross(forward, x_axis)
    y_axis /= np.linalg.norm(y_axis)
    return np.vstack([x_axis, y_axis, forward])


def _project(
    points: np.ndarray,
    rotation: np.ndarray,
    translation: np.ndarray,
    intrinsics: dict[str, float],
) -> tuple[np.ndarray, np.ndarray]:
    camera_points = (rotation @ points.T).T + translation
    depths = camera_points[:, 2]
    safe_depths = np.where(np.abs(depths) < 1.0e-9, 1.0e-9, depths)
    pixels = np.empty((len(points), 2), dtype=np.float64)
    pixels[:, 0] = intrinsics["fx"] * camera_points[:, 0] / safe_depths + intrinsics["cx"]
    pixels[:, 1] = intrinsics["fy"] * camera_points[:, 1] / safe_depths + intrinsics["cy"]
    return pixels, depths


def _make_scene(seed: int, marker_count: int) -> tuple[np.ndarray, list[dict[str, Any]]]:
    rng = np.random.Generator(np.random.PCG64(seed ^ 0x5EED3D))
    indices = np.arange(marker_count, dtype=np.float64)
    z = 1.0 - 2.0 * (indices + 0.5) / marker_count
    radial = np.sqrt(np.maximum(0.0, 1.0 - z * z))
    theta = indices * (math.pi * (3.0 - math.sqrt(5.0))) + 0.37
    radius = 2.05 + 0.45 * rng.random(marker_count)
    points = np.column_stack(
        [radius * radial * np.cos(theta), radius * radial * np.sin(theta), radius * z]
    )
    points += rng.normal(0.0, 0.035, size=points.shape)

    markers: list[dict[str, Any]] = []
    hue_offset = float(rng.random())
    for index, point in enumerate(points):
        hue = (hue_offset + index * 0.6180339887498949) % 1.0
        red, green, blue = colorsys.hsv_to_rgb(hue, 0.95, 0.96)
        glyph = rng.integers(0, 2, size=(5, 5), dtype=np.uint8)
        markers.append(
            {
                "id": f"LM-{index + 1:04d}",
                "xyz": [canonical_float(value) for value in point],
                "rgb": [
                    int(round(red * 255.0)),
                    int(round(green * 255.0)),
                    int(round(blue * 255.0)),
                ],
                "glyph": glyph.tolist(),
            }
        )
    return points, markers


def _draw_scene_geometry(draw: ImageDraw.ImageDraw, width: int, height: int) -> None:
    line_color = (75, 82, 94)
    faint_color = (48, 57, 72)
    margin = 36
    draw.rectangle(
        (margin, margin, width - margin, height - margin), outline=faint_color, width=1
    )
    for fraction in (0.2, 0.4, 0.6, 0.8):
        x = int(width * fraction)
        y = int(height * fraction)
        draw.line((x, margin, x, height - margin), fill=faint_color, width=1)
        draw.line((margin, y, width - margin, y), fill=faint_color, width=1)
    center = (width // 2, height // 2)
    for radius in (90, 150, 210):
        draw.ellipse(
            (center[0] - radius, center[1] - int(radius * 0.62), center[0] + radius, center[1] + int(radius * 0.62)),
            outline=line_color,
            width=2,
        )
    diamond = [
        (center[0], center[1] - 145),
        (center[0] + 190, center[1]),
        (center[0], center[1] + 145),
        (center[0] - 190, center[1]),
    ]
    draw.line(diamond + [diamond[0]], fill=line_color, width=2)


def _draw_marker(
    image: np.ndarray,
    center_x: int,
    center_y: int,
    rgb: tuple[int, int, int],
    glyph: list[list[int]],
) -> None:
    height, width, _ = image.shape
    radius = 9
    x0 = center_x - radius
    y0 = center_y - radius
    x1 = center_x + radius
    y1 = center_y + radius
    if x1 < 0 or y1 < 0 or x0 >= width or y0 >= height:
        return
    clip_x0 = max(0, x0)
    clip_y0 = max(0, y0)
    clip_x1 = min(width - 1, x1)
    clip_y1 = min(height - 1, y1)
    if clip_x0 > clip_x1 or clip_y0 > clip_y1:
        return
    yy, xx = np.mgrid[clip_y0 : clip_y1 + 1, clip_x0 : clip_x1 + 1]
    mask = (xx - center_x) ** 2 + (yy - center_y) ** 2 <= radius * radius
    base = np.asarray(rgb, dtype=np.float64)
    patch = np.broadcast_to(base, (clip_y1 - clip_y0 + 1, clip_x1 - clip_x0 + 1, 3)).copy()
    # A unique 5x5 value code remains highly saturated, so the chromatic blob
    # detector still finds one stable center while descriptors remain distinct.
    glyph_array = np.asarray(glyph, dtype=np.float64)
    cell_w = max(1.0, (radius * 1.55) / 5.0)
    for gy in range(5):
        for gx in range(5):
            value = 0.34 if glyph_array[gy, gx] < 0.5 else 1.0
            left = center_x - (radius * 0.78) + gx * cell_w
            right = left + cell_w
            top = center_y - (radius * 0.78) + gy * cell_w
            bottom = top + cell_w
            cell_mask = (xx >= left) & (xx < right) & (yy >= top) & (yy < bottom)
            patch[cell_mask] = base * value
    region = image[clip_y0 : clip_y1 + 1, clip_x0 : clip_x1 + 1, :3]
    region[mask] = patch[mask]


def generate_sample_dataset(
    output_dir: Path,
    *,
    seed: int = 20261117,
    view_count: int = 8,
    width: int = 640,
    height: int = 480,
    marker_count: int = 72,
) -> dict[str, Any]:
    if view_count < 2:
        raise ValueError("view_count must be at least 2")
    if width < 160 or height < 120:
        raise ValueError("sample resolution is too small")
    output = Path(output_dir)
    views_dir = output / "views"
    cameras_dir = output / "cameras"
    views_dir.mkdir(parents=True, exist_ok=True)
    cameras_dir.mkdir(parents=True, exist_ok=True)

    points, markers = _make_scene(seed, marker_count)
    rng = np.random.Generator(np.random.PCG64(seed ^ 0xC0FFEE))
    base_yaw = float(rng.random()) * math.tau
    target = np.array([0.0, 0.0, 0.08], dtype=np.float64)
    fx = 1.32 * float(width)
    fy = 1.32 * float(height)
    intrinsics = {
        "fx": canonical_float(fx),
        "fy": canonical_float(fy),
        "cx": canonical_float(width / 2.0 - 0.5),
        "cy": canonical_float(height / 2.0 - 0.5),
    }

    view_records: list[dict[str, Any]] = []
    for view_position in range(view_count):
        view_index = view_position + 1
        yaw = base_yaw + math.tau * view_position / view_count
        elevation = math.radians(-9.0 + 18.0 * ((view_position * 3) % max(1, view_count)) / max(1, view_count - 1))
        distance = 6.8 + 0.2 * math.sin(2.0 * yaw + 0.4)
        camera_center = np.array(
            [
                distance * math.cos(elevation) * math.cos(yaw),
                distance * math.cos(elevation) * math.sin(yaw),
                distance * math.sin(elevation),
            ],
            dtype=np.float64,
        )
        rotation = _look_at(camera_center, target)
        translation = -(rotation @ camera_center)

        view_rng = np.random.Generator(np.random.PCG64(seed + 104729 * view_index))
        yy, xx = np.mgrid[0:height, 0:width]
        background = np.empty((height, width, 3), dtype=np.float64)
        background[:, :, 0] = 18.0 + 16.0 * xx / max(1, width - 1)
        background[:, :, 1] = 22.0 + 18.0 * yy / max(1, height - 1)
        background[:, :, 2] = 30.0 + 14.0 * (xx + yy) / max(1, width + height - 2)
        background += view_rng.normal(0.0, 1.7, size=(height, width, 1))
        image_array = np.clip(background, 0.0, 255.0).astype(np.uint8)
        pil_image = Image.fromarray(image_array, mode="RGB")
        draw = ImageDraw.Draw(pil_image)
        _draw_scene_geometry(draw, width, height)
        scene_array = np.asarray(pil_image, dtype=np.uint8).copy()

        projected, depths = _project(points, rotation, translation, intrinsics)
        draw_order = np.argsort(depths)[::-1]
        for marker_index in draw_order:
            depth = float(depths[marker_index])
            x, y = projected[marker_index]
            if depth <= 0.5:
                continue
            if x < 14 or y < 14 or x > width - 15 or y > height - 15:
                continue
            marker = markers[int(marker_index)]
            _draw_marker(
                scene_array,
                int(round(float(x))),
                int(round(float(y))),
                (marker["rgb"][0], marker["rgb"][1], marker["rgb"][2]),
                marker["glyph"],
            )

        final_image = Image.fromarray(scene_array, mode="RGB")
        image_path = views_dir / f"view_{view_index:04d}.png"
        final_image.save(image_path, format="PNG", optimize=False, compress_level=6)
        image_bytes = image_path.read_bytes()
        camera = {
            "schema_version": 1,
            "view_index": view_index,
            "width": width,
            "height": height,
            "intrinsics": intrinsics,
            "rotation": [
                [canonical_float(value) for value in row] for row in rotation.tolist()
            ],
            "translation": [canonical_float(value) for value in translation.tolist()],
            "camera_center": [canonical_float(value) for value in camera_center.tolist()],
            "source": {"kind": "synthetic", "seed": seed, "view_position": view_position},
        }
        camera_path = cameras_dir / f"view_{view_index:04d}.json"
        atomic_write_json(camera_path, camera)
        view_records.append(
            {
                "view_index": view_index,
                "image": f"views/{image_path.name}",
                "camera": f"cameras/{camera_path.name}",
                "image_sha256": sha256_bytes(image_bytes),
                "camera_sha256": sha256_json(camera),
                "width": width,
                "height": height,
            }
        )

    manifest = {
        "schema_version": 1,
        "kind": "synthetic-multiview-sample",
        "seed": seed,
        "width": width,
        "height": height,
        "view_count": view_count,
        "marker_count": marker_count,
        "views": view_records,
        "scene": {
            "landmark_count": marker_count,
            "primitive_types": ["grid", "concentric-ellipses", "diamond", "texture-markers"],
        },
    }
    atomic_write_json(output / "sample_manifest.json", manifest)
    return manifest


def materialize_injections(
    clean_input: Path,
    injected_output: Path,
    injections: InjectionSet,
) -> None:
    source = Path(clean_input)
    target = Path(injected_output)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)

    for item in injections.items:
        if item.type == "corrupt_png" and item.view is not None:
            path = target / "views" / f"view_{item.view:04d}.png"
            atomic_write_bytes(
                path,
                b"\x89PNG\r\n\x1a\n" + b"injected-corrupt-png-payload" + bytes([0]) * 8
            )
        elif item.type == "missing_camera" and item.view is not None:
            path = target / "cameras" / f"view_{item.view:04d}.json"
            if path.exists():
                path.unlink()
        elif item.type == "low_parallax" and item.pair is not None:
            first = item.pair[0]
            second = item.pair[1]
            first_path = target / "cameras" / f"view_{first:04d}.json"
            second_path = target / "cameras" / f"view_{second:04d}.json"
            if first_path.exists() and second_path.exists():
                first_camera = json.loads(first_path.read_text(encoding="utf-8"))
                second_camera = json.loads(second_path.read_text(encoding="utf-8"))
                for field in ("rotation", "translation", "camera_center"):
                    second_camera[field] = first_camera[field]
                atomic_write_json(second_path, second_camera)

    manifest_path = target / "sample_manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()
