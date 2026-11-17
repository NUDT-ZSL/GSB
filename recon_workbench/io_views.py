"""View discovery, PNG validation, camera validation, and manifest hashing."""

from __future__ import annotations

import io
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .canonical import sha256_bytes, sha256_json
from .errors import E_VIEW_INVALID, Failure
from .injection import InjectionSet


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_INDEXED_NAME = re.compile(r"^view_(\d+)\.(png|json)$", re.IGNORECASE)


@dataclass(frozen=True)
class Camera:
    view_index: int
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    rotation: np.ndarray
    translation: np.ndarray
    normalized: dict[str, Any]
    hash: str


@dataclass(frozen=True)
class ViewInput:
    view_index: int
    image_path: Path
    camera_path: Path
    image_hash: str
    camera: Camera
    color: np.ndarray
    gray: np.ndarray


@dataclass(frozen=True)
class ViewManifest:
    views: tuple[ViewInput, ...]
    failures: tuple[Failure, ...]
    discovered_indices: tuple[int, ...]
    camera_hash: str
    input_hash: str
    resolution: tuple[int, int] | None
    image_hashes: dict[int, str]
    camera_hashes: dict[int, str]


def _index_from_path(path: Path) -> int | None:
    match = _INDEXED_NAME.match(path.name)
    if match:
        return int(match.group(1))
    return None


def _find_dirs(input_dir: Path) -> tuple[Path, Path]:
    views_dir = input_dir / "views" if (input_dir / "views").is_dir() else input_dir
    cameras_dir = input_dir / "cameras" if (input_dir / "cameras").is_dir() else input_dir
    return views_dir, cameras_dir


def _camera_from_json(
    payload: dict[str, Any],
    *,
    expected_index: int,
) -> Camera:
    if not isinstance(payload, dict):
        raise ValueError("camera JSON root must be an object")
    view_index = payload.get("view_index")
    if not isinstance(view_index, int) or isinstance(view_index, bool) or view_index < 1:
        raise ValueError("camera view_index must be a positive integer")
    if view_index != expected_index:
        raise ValueError(
            f"camera view_index {view_index} does not match filename index {expected_index}"
        )
    width = payload.get("width")
    height = payload.get("height")
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or width <= 0
        or not isinstance(height, int)
        or isinstance(height, bool)
        or height <= 0
    ):
        raise ValueError("camera width and height must be positive integers")
    intrinsics = payload.get("intrinsics")
    if not isinstance(intrinsics, dict):
        raise ValueError("camera intrinsics must be an object")
    values: dict[str, float] = {}
    for key in ("fx", "fy", "cx", "cy"):
        value = intrinsics.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError(f"camera intrinsics.{key} must be finite")
        values[key] = float(value)
    if values["fx"] <= 0.0 or values["fy"] <= 0.0:
        raise ValueError("camera focal lengths must be positive")

    rotation_raw = payload.get("rotation")
    if not isinstance(rotation_raw, list) or len(rotation_raw) != 3:
        raise ValueError("camera rotation must be a 3x3 matrix")
    rotation = np.asarray(rotation_raw, dtype=np.float64)
    if rotation.shape != (3, 3) or not np.isfinite(rotation).all():
        raise ValueError("camera rotation must contain finite numbers")
    translation_raw = payload.get("translation")
    if not isinstance(translation_raw, list) or len(translation_raw) != 3:
        raise ValueError("camera translation must contain three numbers")
    translation = np.asarray(translation_raw, dtype=np.float64)
    if translation.shape != (3,) or not np.isfinite(translation).all():
        raise ValueError("camera translation must contain finite numbers")
    if abs(float(np.linalg.det(rotation)) - 1.0) > 1.0e-4:
        raise ValueError("camera rotation determinant is not close to 1")
    if not np.allclose(rotation @ rotation.T, np.eye(3), atol=1.0e-5):
        raise ValueError("camera rotation is not orthonormal")

    normalized = {
        "view_index": view_index,
        "width": width,
        "height": height,
        "intrinsics": values,
        "rotation": [
            [round(float(value), 12) for value in row] for row in rotation.tolist()
        ],
        "translation": [round(float(value), 12) for value in translation.tolist()],
    }
    return Camera(
        view_index=view_index,
        width=width,
        height=height,
        fx=values["fx"],
        fy=values["fy"],
        cx=values["cx"],
        cy=values["cy"],
        rotation=rotation,
        translation=translation,
        normalized=normalized,
        hash=sha256_json(normalized),
    )
def load_view_manifest(input_dir: Path, injections: InjectionSet) -> ViewManifest:
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        return ViewManifest(
            views=tuple(),
            failures=(
                Failure(
                    code=E_VIEW_INVALID,
                    stage="features",
                    message=f"input directory does not exist: {input_dir}",
                ),
            ),
            discovered_indices=tuple(),
            camera_hash=sha256_json({"cameras": [], "missing": True}),
            input_hash=sha256_json({"input": str(input_dir), "missing": True}),
            resolution=None,
            image_hashes={},
            camera_hashes={},
        )

    views_dir, cameras_dir = _find_dirs(input_dir)
    image_entries: dict[int, list[Path]] = {}
    camera_entries: dict[int, list[Path]] = {}
    for path in sorted(views_dir.iterdir(), key=lambda item: item.name):
        if path.is_file() and path.suffix.lower() == ".png":
            index = _index_from_path(path)
            if index is not None:
                image_entries.setdefault(index, []).append(path)
    for path in sorted(cameras_dir.iterdir(), key=lambda item: item.name):
        if path.is_file() and path.suffix.lower() == ".json":
            index = _index_from_path(path)
            if index is not None:
                camera_entries.setdefault(index, []).append(path)

    all_indices = sorted(set(image_entries) | set(camera_entries))
    failures: list[Failure] = []
    selected_images: dict[int, Path] = {}
    selected_cameras: dict[int, Path] = {}
    ambiguous: set[int] = set()

    for index in all_indices:
        images = image_entries.get(index, [])
        cameras = camera_entries.get(index, [])
        if len(images) > 1:
            ambiguous.add(index)
            failures.append(
                Failure(
                    code=E_VIEW_INVALID,
                    stage="features",
                    message="duplicate image files for view index",
                    view_index=index,
                    detail={"files": [path.name for path in images]},
                )
            )
        elif len(images) == 1:
            selected_images[index] = images[0]
        if len(cameras) > 1:
            ambiguous.add(index)
            failures.append(
                Failure(
                    code=E_VIEW_INVALID,
                    stage="features",
                    message="duplicate camera parameter files for view index",
                    view_index=index,
                    detail={"files": [path.name for path in cameras]},
                )
            )
        elif len(cameras) == 1:
            selected_cameras[index] = cameras[0]

    decoded: dict[int, dict[str, Any]] = {}
    raw_image_hashes: dict[int, str] = {}
    for index in all_indices:
        path = selected_images.get(index)
        corrupt_item = injections.first_view(index, "corrupt_png")
        if path is None:
            failure = Failure(
                code=E_VIEW_INVALID,
                stage="features",
                message="view image is missing",
                view_index=index,
            )
            failures.append(failure)
            continue
        try:
            raw = path.read_bytes()
        except OSError as error:
            failures.append(
                Failure(
                    code=E_VIEW_INVALID,
                    stage="features",
                    message="view image cannot be read",
                    view_index=index,
                    injection_ids=(corrupt_item.id,) if corrupt_item else (),
                    detail={"error": str(error)},
                )
            )
            continue
        raw_image_hashes[index] = sha256_bytes(raw)
        if corrupt_item is not None:
            failures.append(
                Failure(
                    code=E_VIEW_INVALID,
                    stage="features",
                    message="injected corrupt PNG bytes",
                    view_index=index,
                    injection_ids=(corrupt_item.id,),
                )
            )
            continue
        if not raw.startswith(PNG_SIGNATURE):
            failures.append(
                Failure(
                    code=E_VIEW_INVALID,
                    stage="features",
                    message="file does not have a PNG container header",
                    view_index=index,
                )
            )
            continue
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != "PNG":
                    raise ValueError("decoder did not identify PNG")
                image.load()
                rgb_image = image.convert("RGB")
                gray_image = image.convert("L")
                color = np.asarray(rgb_image, dtype=np.uint8).copy()
                gray = np.asarray(gray_image, dtype=np.uint8).copy()
                width, height = rgb_image.size
        except Exception as error:
            failures.append(
                Failure(
                    code=E_VIEW_INVALID,
                    stage="features",
                    message="PNG image cannot be decoded",
                    view_index=index,
                    detail={"error": type(error).__name__ + ": " + str(error)},
                )
            )
            continue
        decoded[index] = {
            "color": color,
            "gray": gray,
            "width": int(width),
            "height": int(height),
        }

    resolutions = Counter(
        (record["width"], record["height"]) for record in decoded.values()
    )
    expected_resolution: tuple[int, int] | None = None
    if resolutions:
        expected_resolution = sorted(
            resolutions.items(), key=lambda item: (-item[1], item[0][0], item[0][1])
        )[0][0]
        for index, record in sorted(decoded.items()):
            if (record["width"], record["height"]) != expected_resolution:
                failures.append(
                    Failure(
                        code=E_VIEW_INVALID,
                        stage="features",
                        message="image resolution is inconsistent with the common sample resolution",
                        view_index=index,
                        detail={
                            "actual": [record["width"], record["height"]],
                            "expected": list(expected_resolution),
                        },
                    )
                )

    cameras: dict[int, Camera] = {}
    camera_hashes: dict[int, str] = {}
    for index in all_indices:
        if index in ambiguous:
            continue
        path = selected_cameras.get(index)
        missing_item = injections.first_view(index, "missing_camera")
        if path is None or missing_item is not None:
            injection_ids = (missing_item.id,) if missing_item else ()
            failures.append(
                Failure(
                    code=E_VIEW_INVALID,
                    stage="features",
                    message=(
                        "injected missing camera parameters"
                        if missing_item
                        else "camera parameter file is missing"
                    ),
                    view_index=index,
                    injection_ids=injection_ids,
                )
            )
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            camera = _camera_from_json(payload, expected_index=index)
        except Exception as error:
            failures.append(
                Failure(
                    code=E_VIEW_INVALID,
                    stage="features",
                    message="camera parameters are missing or invalid",
                    view_index=index,
                    detail={"error": type(error).__name__ + ": " + str(error)},
                )
            )
            continue
        image_record = decoded.get(index)
        if image_record is not None and (
            camera.width != image_record["width"] or camera.height != image_record["height"]
        ):
            failures.append(
                Failure(
                    code=E_VIEW_INVALID,
                    stage="features",
                    message="camera resolution does not match the decoded image",
                    view_index=index,
                    detail={
                        "camera": [camera.width, camera.height],
                        "image": [image_record["width"], image_record["height"]],
                    },
                )
            )
            continue
        cameras[index] = camera
        camera_hashes[index] = camera.hash

    invalid_views = {failure.view_index for failure in failures if failure.view_index is not None}
    views: list[ViewInput] = []
    for index in all_indices:
        if index in invalid_views:
            continue
        image_record = decoded.get(index)
        camera = cameras.get(index)
        image_path = selected_images.get(index)
        camera_path = selected_cameras.get(index)
        if image_record is None or camera is None or image_path is None or camera_path is None:
            continue
        views.append(
            ViewInput(
                view_index=index,
                image_path=image_path,
                camera_path=camera_path,
                image_hash=raw_image_hashes[index],
                camera=camera,
                color=image_record["color"],
                gray=image_record["gray"],
            )
        )

    camera_hash = sha256_json(
        {
            "cameras": [
                {"view_index": index, "hash": camera_hashes.get(index, "MISSING")}
                for index in all_indices
            ]
        }
    )
    input_hash = sha256_json(
        {
            "images": [
                {"view_index": index, "hash": raw_image_hashes.get(index, "MISSING")}
                for index in all_indices
            ],
            "camera_hash": camera_hash,
        }
    )
    return ViewManifest(
        views=tuple(views),
        failures=tuple(failures),
        discovered_indices=tuple(all_indices),
        camera_hash=camera_hash,
        input_hash=input_hash,
        resolution=expected_resolution,
        image_hashes=raw_image_hashes,
        camera_hashes=camera_hashes,
    )
