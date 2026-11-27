"""Foreground extraction and serializable Poisson boundary/gradient fields."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .errors import FusionError
from .geometry import inner_boundary, validate_mask
from .io_utils import array_descriptor, save_json, save_npy


@dataclass
class FieldBundle:
    foreground_rgb: np.ndarray
    valid_mask: np.ndarray
    gradient_x: np.ndarray
    gradient_y: np.ndarray
    boundary_mask: np.ndarray
    boundary_rgb: np.ndarray
    unknown_mask: np.ndarray
    extraction: dict[str, Any] = field(default_factory=dict)


def _nearest_fill(rgb: np.ndarray, valid: np.ndarray, target: np.ndarray) -> np.ndarray:
    if not valid.any():
        raise FusionError(
            "E_FOREGROUND_EXTRACTION",
            "前景图中没有有效零件像素",
            {"valid_pixels": 0},
        )
    if np.all(valid[target]):
        return rgb.copy()

    height, width = target.shape
    filled = rgb.copy()
    known = valid.copy()
    queue: deque[tuple[int, int]] = deque((int(y), int(x)) for y, x in np.argwhere(valid))
    if not np.any(valid & target):
        raise FusionError(
            "E_FOREGROUND_EXTRACTION",
            "目标融合区域内没有有效前景像素",
            {
                "valid_pixels": int(np.count_nonzero(valid)),
                "target_pixels": int(np.count_nonzero(target)),
            },
        )

    while queue and not np.all(known[target]):
        y, x = queue.popleft()
        for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= ny < height and 0 <= nx < width and not known[ny, nx]:
                known[ny, nx] = True
                filled[ny, nx] = filled[y, x]
                queue.append((ny, nx))
    if not np.all(known[target]):
        raise FusionError(
            "E_FOREGROUND_EXTRACTION",
            "无法将有效前景像素填充到整个目标区域",
            {"unfilled_pixels": int(np.count_nonzero(target & ~known))},
        )
    return filled


def _infer_solid_background(rgb: np.ndarray) -> np.ndarray:
    border = np.concatenate(
        (rgb[0, :, :], rgb[-1, :, :], rgb[:, 0, :], rgb[:, -1, :]), axis=0
    )
    return np.median(border, axis=0)


def _validate_extracted_shape(
    base: np.ndarray, foreground: np.ndarray, mask: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    if base.ndim != 3 or base.shape[2] != 3:
        raise FusionError(
            "E_INVALID_IMAGE",
            "底板必须为 H×W×3 RGB 图像",
            {"shape": list(base.shape)},
        )
    if foreground.shape[:2] != base.shape[:2]:
        raise FusionError(
            "E_DIMENSIONS",
            "前景图与底板尺寸不一致",
            {
                "foreground_shape": list(foreground.shape),
                "base_shape": list(base.shape),
            },
        )
    if mask.shape != base.shape[:2]:
        raise FusionError(
            "E_DIMENSIONS",
            "掩码与底板尺寸不一致",
            {"mask_shape": list(mask.shape), "base_shape": list(base.shape)},
        )
    return base, foreground


def build_fields(
    base: np.ndarray,
    foreground: np.ndarray,
    mask: np.ndarray,
    *,
    foreground_mode: str = "auto",
    background_color: list[float] | tuple[float, float, float] | None = None,
    alpha_threshold: float = 1.0,
    solid_threshold: float = 8.0,
) -> FieldBundle:
    base = np.asarray(base, dtype=np.float64)
    foreground = np.asarray(foreground, dtype=np.float64)
    mask = np.asarray(mask, dtype=bool)
    _validate_extracted_shape(base, foreground, mask)
    diagnostics = validate_mask(mask, base.shape[:2])

    if foreground_mode not in {"auto", "alpha", "solid"}:
        raise FusionError(
            "E_INVALID_IMAGE",
            f"未知前景提取模式: {foreground_mode}",
            {"foreground_mode": foreground_mode},
        )

    channels = foreground.shape[2]
    if channels not in (3, 4):
        raise FusionError(
            "E_INVALID_IMAGE",
            "前景图必须为 RGB 或 RGBA",
            {"shape": list(foreground.shape)},
        )
    if not np.all(np.isfinite(foreground)):
        location = np.argwhere(~np.isfinite(foreground))
        y, x = int(location[0, 0]), int(location[0, 1])
        raise FusionError(
            "E_NONFINITE_INPUT",
            "前景图包含非有限值",
            {"array": "foreground", "first_pixel": [y, x]},
        )
    if not np.all(np.isfinite(base)):
        location = np.argwhere(~np.isfinite(base))
        y, x = int(location[0, 0]), int(location[0, 1])
        raise FusionError(
            "E_NONFINITE_INPUT",
            "底板包含非有限值",
            {"array": "base", "first_pixel": [y, x]},
        )

    rgb = foreground[:, :, :3]
    mode = foreground_mode
    if mode == "auto":
        mode = "alpha" if channels == 4 and np.any(foreground[:, :, 3] < 254.5) else "solid"

    if mode == "alpha":
        if channels != 4:
            raise FusionError(
                "E_FOREGROUND_EXTRACTION",
                "alpha 模式需要 RGBA 前景图",
                {"channels": channels},
            )
        valid = foreground[:, :, 3] > float(alpha_threshold)
        extraction = {
            "mode": "alpha",
            "alpha_threshold": float(alpha_threshold),
            "valid_pixels": int(np.count_nonzero(valid)),
        }
        background_rgb = None
    else:
        if background_color is None:
            background_rgb = _infer_solid_background(rgb)
        else:
            background_rgb = np.asarray(background_color, dtype=np.float64).reshape(3)
        distance = np.sqrt(np.sum((rgb - background_rgb[None, None, :]) ** 2, axis=2))
        valid = distance > float(solid_threshold)
        extraction = {
            "mode": "solid",
            "background_rgb": [round(float(v), 6) for v in background_rgb],
            "solid_threshold": float(solid_threshold),
            "valid_pixels": int(np.count_nonzero(valid)),
        }
    extraction["valid_in_mask_pixels"] = int(np.count_nonzero(valid & mask))

    filled_rgb = _nearest_fill(rgb, valid, mask)
    extraction["nearest_fill_pixels"] = int(np.count_nonzero(mask & ~valid))
    extraction["filled_rgb_finite"] = bool(np.all(np.isfinite(filled_rgb)))

    gradient_x = np.zeros_like(filled_rgb)
    gradient_y = np.zeros_like(filled_rgb)
    gradient_x[:, :-1, :] = filled_rgb[:, 1:, :] - filled_rgb[:, :-1, :]
    gradient_y[:-1, :, :] = filled_rgb[1:, :, :] - filled_rgb[:-1, :, :]

    boundary = inner_boundary(mask)
    unknown = mask & ~boundary
    boundary_rgb = np.zeros_like(base)
    boundary_rgb[boundary] = base[boundary]

    extraction["mask_diagnostics"] = diagnostics
    extraction["mask_pixels"] = int(np.count_nonzero(mask))
    extraction["interior_pixels"] = int(np.count_nonzero(unknown))
    extraction["boundary_pixels"] = int(np.count_nonzero(boundary))
    return FieldBundle(
        foreground_rgb=filled_rgb,
        valid_mask=valid,
        gradient_x=gradient_x,
        gradient_y=gradient_y,
        boundary_mask=boundary,
        boundary_rgb=boundary_rgb,
        unknown_mask=unknown,
        extraction=extraction,
    )


def save_fields(bundle: FieldBundle, output_dir: str | Path) -> dict[str, Any]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    arrays = {
        "foreground_rgb": bundle.foreground_rgb.astype(np.float32),
        "valid_mask": bundle.valid_mask.astype(np.uint8),
        "gradient_x": bundle.gradient_x.astype(np.float32),
        "gradient_y": bundle.gradient_y.astype(np.float32),
        "boundary_mask": bundle.boundary_mask.astype(np.uint8),
        "boundary_rgb": bundle.boundary_rgb.astype(np.float32),
        "unknown_mask": bundle.unknown_mask.astype(np.uint8),
    }
    descriptors: dict[str, Any] = {}
    for name, array in arrays.items():
        filename = f"{name}.npy"
        save_npy(destination / filename, array)
        descriptors[name] = {"file": filename, **array_descriptor(array)}
    manifest = {
        "schema_version": 1,
        "description": "Serialized boundary conditions and target foreground gradient field.",
        "gradient_convention": "forward differences: gx[y,x]=F[y,x+1]-F[y,x], gy[y,x]=F[y+1,x]-F[y,x]",
        "boundary_condition": {
            "file": "boundary_rgb.npy",
            "definition": "base RGB on the one-pixel inner mask boundary",
            **descriptors["boundary_rgb"],
        },
        "gradient_field": {
            "gx_file": "gradient_x.npy",
            "gy_file": "gradient_y.npy",
            "gx": descriptors["gradient_x"],
            "gy": descriptors["gradient_y"],
        },
        "foreground_extraction": bundle.extraction,
        "arrays": descriptors,
    }
    save_json(destination / "fields_manifest.json", manifest)
    return manifest
