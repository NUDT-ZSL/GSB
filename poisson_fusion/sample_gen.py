"""Fixed-seed generation of all fusion inputs, including invalid-mask fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .geometry import connected_components_4, inner_boundary, make_band, validate_mask
from .io_utils import (
    array_descriptor,
    array_sha256,
    save_json,
    save_npy,
    write_png,
)


def _resize_bilinear(source: np.ndarray, height: int, width: int) -> np.ndarray:
    if height == 1:
        ys = np.zeros(1, dtype=np.float64)
    else:
        ys = np.linspace(0.0, source.shape[0] - 1.0, height)
    if width == 1:
        xs = np.zeros(1, dtype=np.float64)
    else:
        xs = np.linspace(0.0, source.shape[1] - 1.0, width)
    y0 = np.floor(ys).astype(np.int64)
    x0 = np.floor(xs).astype(np.int64)
    y1 = np.minimum(y0 + 1, source.shape[0] - 1)
    x1 = np.minimum(x0 + 1, source.shape[1] - 1)
    wy = (ys - y0)[:, None]
    wx = (xs - x0)[None, :]
    top = source[y0[:, None], x0[None, :]] * (1.0 - wx) + source[y0[:, None], x1[None, :]] * wx
    bottom = source[y1[:, None], x0[None, :]] * (1.0 - wx) + source[y1[:, None], x1[None, :]] * wx
    return top * (1.0 - wy) + bottom * wy


def _value_noise(rng: np.random.Generator, height: int, width: int, cell: int) -> np.ndarray:
    coarse_h = height // cell + 3
    coarse_w = width // cell + 3
    coarse = rng.random((coarse_h, coarse_w))
    return _resize_bilinear(coarse, height, width)


def _make_base(rng: np.random.Generator, height: int, width: int) -> np.ndarray:
    yy, xx = np.mgrid[0:height, 0:width]
    xn = xx / max(width - 1, 1)
    yn = yy / max(height - 1, 1)
    grain = _value_noise(rng, height, width, 11)
    weave = _value_noise(rng, height, width, 29)
    fine = rng.normal(0.0, 1.0, size=(height, width))
    light_center_x = 0.25 + 0.5 * rng.random()
    light_center_y = 0.25 + 0.5 * rng.random()
    radial = np.exp(
        -(
            ((xn - light_center_x) / 0.58) ** 2
            + ((yn - light_center_y) / 0.48) ** 2
        )
    )
    diagonal = 0.5 + 0.5 * np.sin(2.0 * np.pi * (0.7 * xn + 0.45 * yn) + rng.uniform(0, 2 * np.pi))

    palette = np.array(
        [
            [44.0, 72.0, 96.0],
            [112.0, 86.0, 62.0],
            [62.0, 104.0, 81.0],
            [118.0, 73.0, 105.0],
        ]
    )
    first = palette[int(rng.integers(0, len(palette)))]
    second = palette[int(rng.integers(0, len(palette)))]
    blend = (0.60 * grain + 0.40 * weave)[:, :, None]
    color = first[None, None, :] * (1.0 - blend) + second[None, None, :] * blend
    lighting = (0.46 + 0.48 * radial + 0.10 * diagonal)[:, :, None]
    stripes = 8.0 * np.sin(2.0 * np.pi * (6.0 * xn + 2.0 * yn))[:, :, None]
    texture = 9.0 * (grain - 0.5)[:, :, None] + 5.0 * (weave - 0.5)[:, :, None]
    speckle = 2.5 * fine[:, :, None]
    base = color * lighting + stripes + texture + speckle
    return np.clip(np.rint(base), 0, 255).astype(np.uint8)


def _ellipse_mask(
    height: int,
    width: int,
    center_x: float,
    center_y: float,
    radius_x: float,
    radius_y: float,
    phase: float,
    wobble: float,
) -> np.ndarray:
    yy, xx = np.mgrid[0:height, 0:width]
    dx = (xx - center_x) / radius_x
    dy = (yy - center_y) / radius_y
    angle = np.arctan2(dy, dx)
    radius = np.sqrt(dx * dx + dy * dy)
    limit = 0.93 + wobble * np.sin(3.0 * angle + phase) + 0.04 * np.cos(5.0 * angle - phase)
    return radius <= limit


def _ensure_safe_mask(mask: np.ndarray) -> np.ndarray:
    if len(connected_components_4(mask)) != 1:
        raise RuntimeError("generated mask is not a single component")
    validate_mask(mask, mask.shape)
    return mask


def _make_foreground(
    rng: np.random.Generator,
    base: np.ndarray,
    mask: np.ndarray,
    variant: int,
) -> np.ndarray:
    height, width = mask.shape
    yy, xx = np.mgrid[0:height, 0:width]
    ys, xs = np.nonzero(mask)
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    xn = (xx - x0) / max(x1 - x0, 1)
    yn = (yy - y0) / max(y1 - y0, 1)

    if variant == 0:
        color_a = np.array([224.0, 91.0, 63.0]) + rng.uniform(-18, 18, size=3)
        color_b = np.array([246.0, 199.0, 76.0]) + rng.uniform(-18, 18, size=3)
        ramp = np.clip(0.18 + 0.78 * xn - 0.28 * yn, 0, 1)
        rgb = color_a[None, None, :] * (1.0 - ramp[:, :, None]) + color_b[None, None, :] * ramp[:, :, None]
        rgb += 28.0 * np.sin(2.0 * np.pi * (3.2 * xn - 2.1 * yn))[:, :, None]
        rgb += 13.0 * np.cos(2.0 * np.pi * (7.0 * yn))[:, :, None]
    else:
        color_a = np.array([72.0, 213.0, 189.0]) + rng.uniform(-18, 18, size=3)
        color_b = np.array([59.0, 96.0, 222.0]) + rng.uniform(-18, 18, size=3)
        ramp = np.clip(0.15 + 0.72 * (1.0 - xn) + 0.24 * yn, 0, 1)
        rgb = color_a[None, None, :] * (1.0 - ramp[:, :, None]) + color_b[None, None, :] * ramp[:, :, None]
        rgb += 24.0 * np.cos(2.0 * np.pi * (2.6 * xn + 3.8 * yn))[:, :, None]
        rgb += 17.0 * np.sin(2.0 * np.pi * (6.2 * xn + 0.7 * yn))[:, :, None]

    # A physically plausible highlight and a darker rim retain internal gradients.
    radial = np.sqrt(((xn - 0.38) / 0.65) ** 2 + ((yn - 0.34) / 0.72) ** 2)
    rgb += (20.0 * np.exp(-2.3 * radial * radial))[:, :, None]
    rgb -= (10.0 * np.clip(radial - 0.78, 0, 1))[:, :, None]
    rgb = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
    alpha = np.where(mask, 255, 0).astype(np.uint8)
    rgba = np.dstack((rgb, alpha))
    return rgba


def _solid_version(rgba: np.ndarray, background: tuple[int, int, int]) -> np.ndarray:
    rgb = rgba[:, :, :3].copy()
    background_mask = rgba[:, :, 3] == 0
    rgb[background_mask] = np.asarray(background, dtype=np.uint8)
    return rgb


def _save_mask(path_npy: Path, path_png: Path, mask: np.ndarray) -> None:
    save_npy(path_npy, mask.astype(np.uint8))
    write_png(path_png, mask.astype(np.uint8) * 255)


def generate_sample(
    seed: int,
    output_dir: str | Path,
    *,
    height: int = 192,
    width: int = 256,
    band_radius: int = 3,
) -> dict[str, Any]:
    """Generate deterministic base, RGBA/solid foregrounds, masks and bands."""

    if height < 96 or width < 96:
        raise ValueError("height and width must both be at least 96")
    if band_radius < 0:
        raise ValueError("band_radius must be non-negative")
    seed = int(seed)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)

    base = _make_base(rng, height, width)
    margin_x = max(30.0, width * 0.16)
    margin_y = max(26.0, height * 0.17)
    center_x = float(rng.uniform(margin_x, width - margin_x))
    center_y = float(rng.uniform(margin_y, height - margin_y))
    radius_x = float(rng.uniform(width * 0.14, width * 0.20))
    radius_y = float(rng.uniform(height * 0.17, height * 0.24))
    phase = float(rng.uniform(0, 2.0 * np.pi))
    mask = _ensure_safe_mask(
        _ellipse_mask(height, width, center_x, center_y, radius_x, radius_y, phase, 0.075)
    )

    shift_x = float(rng.uniform(-0.035, 0.035) * width)
    shift_y = float(rng.uniform(-0.025, 0.025) * height)
    mask_v2 = _ensure_safe_mask(
        _ellipse_mask(
            height,
            width,
            center_x + shift_x,
            center_y + shift_y,
            radius_x * 1.06,
            radius_y * 0.96,
            phase + 0.20,
            0.095,
        )
    )

    foreground_rgba = _make_foreground(rng, base, mask, variant=0)
    foreground_v2_rgba = _make_foreground(rng, base, mask_v2, variant=1)
    solid_background = (18, 42, 57)
    foreground_solid = _solid_version(foreground_rgba, solid_background)
    foreground_v2_solid = _solid_version(foreground_v2_rgba, solid_background)

    boundary = inner_boundary(mask)
    boundary_v2 = inner_boundary(mask_v2)
    band = make_band(boundary, band_radius)
    band_v2 = make_band(boundary_v2, band_radius)

    arrays: dict[str, np.ndarray] = {
        "base": base,
        "foreground_rgba": foreground_rgba,
        "foreground_solid": foreground_solid,
        "foreground_v2_rgba": foreground_v2_rgba,
        "foreground_v2_solid": foreground_v2_solid,
        "target_mask": mask.astype(np.uint8),
        "target_mask_v2": mask_v2.astype(np.uint8),
        "boundary_ring": boundary.astype(np.uint8),
        "boundary_band": band.astype(np.uint8),
        "boundary_ring_v2": boundary_v2.astype(np.uint8),
        "boundary_band_v2": band_v2.astype(np.uint8),
    }

    files: dict[str, Any] = {}
    for name, array in arrays.items():
        npy_path = destination / f"{name}.npy"
        save_npy(npy_path, array)
        files[name] = {
            "npy": npy_path.name,
            **array_descriptor(array),
        }

    image_pngs = {
        "base": base,
        "foreground_rgba": foreground_rgba,
        "foreground_solid": foreground_solid,
        "foreground_v2_rgba": foreground_v2_rgba,
        "foreground_v2_solid": foreground_v2_solid,
        "target_mask": mask.astype(np.uint8) * 255,
        "target_mask_v2": mask_v2.astype(np.uint8) * 255,
        "boundary_ring": boundary.astype(np.uint8) * 255,
        "boundary_band": band.astype(np.uint8) * 255,
        "boundary_ring_v2": boundary_v2.astype(np.uint8) * 255,
        "boundary_band_v2": band_v2.astype(np.uint8) * 255,
    }
    for name, image in image_pngs.items():
        png_path = destination / f"{name}.png"
        write_png(png_path, image)
        files[name]["png"] = png_path.name

    broken_mask = mask.copy()
    corridor_y = min(range(height), key=lambda y: abs(y - int(center_y)) if np.any(mask[y]) else height + 1)
    row = np.nonzero(mask[corridor_y])[0]
    if row.size:
        broken_mask[corridor_y, : int(row.min()) + 1] = True
    invalid_dir = destination / "invalid"
    _save_mask(invalid_dir / "empty_mask.npy", invalid_dir / "empty_mask.png", np.zeros_like(mask))
    _save_mask(invalid_dir / "full_mask.npy", invalid_dir / "full_mask.png", np.ones_like(mask))
    _save_mask(invalid_dir / "broken_mask.npy", invalid_dir / "broken_mask.png", broken_mask)
    save_npy(invalid_dir / "mismatched_foreground.npy", foreground_rgba[:, :-5, :])
    save_json(
        invalid_dir / "README.json",
        {
            "empty_mask": "E_EMPTY_MASK / 2001",
            "full_mask": "E_FULL_MASK / 2002",
            "broken_mask": "E_BROKEN_BOUNDARY / 2003 (corridor reaches image edge)",
            "mismatched_foreground": "E_DIMENSIONS / 1101",
        },
    )

    metadata = {
        "schema_version": 1,
        "generator": "poisson_fusion.sample_gen",
        "seed": seed,
        "shape": [height, width],
        "band_radius": band_radius,
        "mask_center": [round(center_x, 6), round(center_y, 6)],
        "mask_radii": [round(radius_x, 6), round(radius_y, 6)],
        "solid_background_rgb": list(solid_background),
        "mask_pixels": int(np.count_nonzero(mask)),
        "mask_v2_pixels": int(np.count_nonzero(mask_v2)),
        "boundary_pixels": int(np.count_nonzero(boundary)),
        "band_pixels": int(np.count_nonzero(band)),
        "foreground_rgba_sha256": array_sha256(foreground_rgba),
        "foreground_v2_rgba_sha256": array_sha256(foreground_v2_rgba),
        "mask_sha256": array_sha256(mask.astype(np.uint8)),
        "mask_v2_sha256": array_sha256(mask_v2.astype(np.uint8)),
        "base_sha256": array_sha256(base),
        "files": files,
    }
    save_json(destination / "sample_metadata.json", metadata)
    return metadata
