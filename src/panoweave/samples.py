"""Deterministic local sample-scene generation and loading."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .config import AppConfig
from .formats import write_scene
from .geometry import derive_homography, invert_radial_normalized
from .model import SceneParameters, ViewParameters, qfloat
from .pngutil import write_png


def _file_sha(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class SampleOptions:
    seed: int = 20261123
    canvas_width: int = 320
    canvas_height: int = 180
    view_count: int = 3
    focal: float = 300.0
    distortion_k1: float = 0.015
    distortion_k2: float = -0.003
    feather_radius: float = 8.0
    camera_spacing: float = 0.28
    gains: tuple[float, ...] = (1.06, 0.94, 1.0)


@dataclass(frozen=True)
class GeneratedSample:
    root: Path
    scene: SceneParameters
    manifest: dict[str, Any]
    files: dict[str, str] = field(default_factory=dict)


def _texture(width: int, height: int, seed: int) -> np.ndarray:
    yy, xx = np.mgrid[0:height, 0:width]
    checker = ((xx // 24 + yy // 24) % 2).astype(np.float64)
    r = (34.0 + (xx * 5.0 + seed * 3) % 172.0 + checker * 26.0) / 255.0
    g = (25.0 + (yy * 7.0 + seed * 5) % 160.0 + (1.0 - checker) * 30.0) / 255.0
    b = (42.0 + ((xx + 2.0 * yy) * 3.0 + seed * 7) % 154.0) / 255.0
    texture = np.stack((r, g, b), axis=-1)
    circles = [
        (int(width * 0.20), int(height * 0.35), 28, (0.95, 0.35, 0.22)),
        (int(width * 0.51), int(height * 0.62), 36, (0.20, 0.82, 0.55)),
        (int(width * 0.78), int(height * 0.28), 24, (0.24, 0.45, 0.96)),
    ]
    for offset, (center_x, center_y, radius, color) in enumerate(circles):
        shift = (seed + offset * 17) % 13 - 6
        center_x = int(np.clip(center_x + shift, 0, width - 1))
        mask = (
            (xx - center_x) * (xx - center_x)
            + (yy - center_y) * (yy - center_y)
            <= radius * radius
        )
        texture[mask] = np.asarray(color, dtype=np.float64)
    grid = ((xx % 40 == 0) | (yy % 40 == 0)).astype(np.float64)
    texture = texture * (1.0 - 0.12 * grid[:, :, None])
    return np.clip(texture, 0.0, 1.0)


def _bilinear_texture(
    texture: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    valid: np.ndarray,
) -> np.ndarray:
    height, width, _ = texture.shape
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    x1 = np.clip(x0 + 1, 0, width - 1)
    y1 = np.clip(y0 + 1, 0, height - 1)
    x0 = np.clip(x0, 0, width - 1)
    y0 = np.clip(y0, 0, height - 1)
    wx = np.where(valid, x - x0, 0.0)[:, :, None]
    wy = np.where(valid, y - y0, 0.0)[:, :, None]
    top = texture[y0, x0] * (1.0 - wx) + texture[y0, x1] * wx
    bottom = texture[y1, x0] * (1.0 - wx) + texture[y1, x1] * wx
    result = top * (1.0 - wy) + bottom * wy
    return np.where(valid[:, :, None], result, 0.0)


def build_scene(options: SampleOptions) -> SceneParameters:
    views: list[ViewParameters] = []
    center = (options.view_count - 1) / 2.0
    for camera_id in range(options.view_count):
        translation_x = (camera_id - center) * options.camera_spacing
        translation_y = ((camera_id % 2) * 2 - 1) * 0.01
        extrinsic = (
            1.0,
            0.0,
            0.0,
            qfloat(translation_x),
            0.0,
            1.0,
            0.0,
            qfloat(translation_y),
            0.0,
            0.0,
            1.0,
            1.0,
        )
        principal_x = (options.canvas_width - 1) / 2.0
        principal_y = (options.canvas_height - 1) / 2.0
        homography = derive_homography(
            options.focal,
            principal_x,
            principal_y,
            extrinsic,
            options.canvas_width,
            options.canvas_height,
        )
        gain = (
            options.gains[camera_id]
            if camera_id < len(options.gains)
            else 1.0
        )
        views.append(
            ViewParameters(
                camera_id=camera_id,
                focal=qfloat(options.focal),
                principal_x=qfloat(principal_x),
                principal_y=qfloat(principal_y),
                extrinsic=extrinsic,
                distortion_k1=qfloat(options.distortion_k1),
                distortion_k2=qfloat(options.distortion_k2),
                exposure_gain=qfloat(gain),
                homography=homography,
                overlap_weight=1.0,
                exposure_mode="linear",
                color_gain=(1.0, 1.0, 1.0),
            )
        )
    return SceneParameters(
        canvas_width=options.canvas_width,
        canvas_height=options.canvas_height,
        feather_radius=qfloat(options.feather_radius),
        color_space="linear_rgb",
        views=tuple(views),
        interpolation="bilinear",
        weight_tolerance=1e-9,
    )


def render_sample_views(
    scene: SceneParameters, seed: int
) -> tuple[dict[int, np.ndarray], np.ndarray]:
    texture = _texture(scene.canvas_width, scene.canvas_height, seed)
    images: dict[int, np.ndarray] = {}
    for view in scene.sorted_views:
        yy, xx = np.mgrid[0 : scene.canvas_height, 0 : scene.canvas_width]
        normalized_x = (xx.astype(np.float64) - view.principal_x) / view.focal
        normalized_y = (yy.astype(np.float64) - view.principal_y) / view.focal
        undistorted_x, undistorted_y = invert_radial_normalized(
            normalized_x,
            normalized_y,
            view.distortion_k1,
            view.distortion_k2,
        )
        undistorted_px = undistorted_x * view.focal + view.principal_x
        undistorted_py = undistorted_y * view.focal + view.principal_y
        homography = view.homography_matrix
        denominator = (
            homography[2, 0] * undistorted_px
            + homography[2, 1] * undistorted_py
            + homography[2, 2]
        )
        safe = np.where(np.abs(denominator) < 1e-15, np.nan, denominator)
        panorama_x = (
            homography[0, 0] * undistorted_px
            + homography[0, 1] * undistorted_py
            + homography[0, 2]
        ) / safe
        panorama_y = (
            homography[1, 0] * undistorted_px
            + homography[1, 1] * undistorted_py
            + homography[1, 2]
        ) / safe
        valid = (
            np.isfinite(panorama_x)
            & np.isfinite(panorama_y)
            & (panorama_x >= 0.0)
            & (panorama_x <= scene.canvas_width - 1)
            & (panorama_y >= 0.0)
            & (panorama_y <= scene.canvas_height - 1)
        )
        sampled = _bilinear_texture(texture, panorama_x, panorama_y, valid)
        exposure = sampled * np.asarray(view.color_gain) * view.exposure_gain
        image = np.floor(np.clip(exposure, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
        image = np.where(valid[:, :, None], image, 0).astype(np.uint8)
        images[view.camera_id] = image
    return images, texture


def _write_manifest(
    root: Path,
    options: SampleOptions,
    scene: SceneParameters,
    files: dict[str, str],
) -> dict[str, Any]:
    manifest = {
        "schema": 1,
        "seed": options.seed,
        "options": {
            **asdict(options),
            "gains": list(options.gains),
        },
        "canvas": {
            "width": scene.canvas_width,
            "height": scene.canvas_height,
        },
        "interpolation": scene.interpolation,
        "semantic_hash": scene.semantic_hash,
        "params": {
            "v1": "params_v1.txt",
            "v2": "params_v2.txt",
            "v3": "params_v3.json",
        },
        "views": [
            {
                "camera_id": view.camera_id,
                "image": f"views/view_{view.camera_id:03d}.npy",
                "preview": f"views/view_{view.camera_id:03d}.png",
                "exposure_gain": view.exposure_gain,
            }
            for view in scene.sorted_views
        ],
        "files": dict(sorted(files.items())),
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def generate_sample(
    root: str | Path,
    options: SampleOptions | None = None,
    *,
    config: AppConfig | None = None,
    clean: bool = True,
) -> GeneratedSample:
    options = options or SampleOptions()
    config = config or AppConfig()
    target = Path(root)
    if clean and target.exists():
        shutil.rmtree(target)
    (target / "views").mkdir(parents=True, exist_ok=True)
    scene = build_scene(options)
    images, texture = render_sample_views(scene, options.seed)
    files: dict[str, str] = {}
    for camera_id, image in images.items():
        npy_path = target / "views" / f"view_{camera_id:03d}.npy"
        png_path = target / "views" / f"view_{camera_id:03d}.png"
        np.save(npy_path, image, allow_pickle=False)
        write_png(png_path, image)
        files[str(npy_path.relative_to(target)).replace("\\", "/")] = _file_sha(npy_path)
        files[str(png_path.relative_to(target)).replace("\\", "/")] = _file_sha(png_path)
    ground_truth_png = target / "ground_truth.png"
    ground_truth_u8 = np.floor(np.clip(texture, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    write_png(ground_truth_png, ground_truth_u8)
    files["ground_truth.png"] = _file_sha(ground_truth_png)
    params = {
        "v1": target / "params_v1.txt",
        "v2": target / "params_v2.txt",
        "v3": target / "params_v3.json",
    }
    write_scene(params["v1"], scene, "v1")
    write_scene(params["v2"], scene, "v2")
    write_scene(params["v3"], scene, "v3")
    for version, path in params.items():
        files[str(path.relative_to(target)).replace("\\", "/")] = _file_sha(path)
    config_path = target / "config.json"
    config_data = config.to_dict()
    config_data["parse_defaults"].update(
        {
            "canvas_width": scene.canvas_width,
            "canvas_height": scene.canvas_height,
            "feather_radius": scene.feather_radius,
            "color_space": scene.color_space,
            "interpolation": scene.interpolation,
            "weight_tolerance": scene.weight_tolerance,
        }
    )
    config_path.write_text(
        json.dumps(config_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    files["config.json"] = _file_sha(config_path)
    injection_path = target / "injections.json"
    injection_path.write_text(
        json.dumps(
            {
                "injections": [
                    {"stage": "parse", "kind": "extrinsic_dimension", "view_id": 0},
                    {"stage": "geometry", "kind": "singular_homography", "view_id": 0},
                    {"stage": "gain", "kind": "zero_gain", "view_id": 1},
                    {"stage": "blend", "kind": "negative_feather"},
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    files["injections.json"] = _file_sha(injection_path)
    manifest = _write_manifest(target, options, scene, files)
    return GeneratedSample(target, scene, manifest, files)


def load_sample_images(root: str | Path, manifest: dict[str, Any] | None = None) -> dict[int, np.ndarray]:
    target = Path(root)
    if manifest is None:
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    images: dict[int, np.ndarray] = {}
    for item in manifest["views"]:
        camera_id = int(item["camera_id"])
        images[camera_id] = np.load(target / item["image"], allow_pickle=False)
    return images


def load_manifest(root: str | Path) -> dict[str, Any]:
    return json.loads((Path(root) / "manifest.json").read_text(encoding="utf-8"))


def parameter_text(root: str | Path, version: str) -> str:
    names = {"v1": "params_v1.txt", "v2": "params_v2.txt", "v3": "params_v3.json"}
    return (Path(root) / names[version]).read_text(encoding="utf-8")

