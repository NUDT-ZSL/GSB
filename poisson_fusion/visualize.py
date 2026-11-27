"""Small, dependency-free visualisations for review artifacts."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .geometry import inner_boundary
from .io_utils import write_png


def mask_png(mask: np.ndarray) -> np.ndarray:
    return (np.asarray(mask, dtype=bool).astype(np.uint8) * 255)


def heatmap_rgb(values: np.ndarray, *, vmax: float | None = None) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    if vmax is None:
        vmax = float(np.max(array)) if array.size else 0.0
    if vmax <= 0:
        return np.zeros((*array.shape, 3), dtype=np.uint8)
    normalized = np.clip(array / vmax, 0.0, 1.0)
    red = np.clip(3.0 * normalized, 0, 1)
    green = np.clip(3.0 * normalized - 1.0, 0, 1)
    blue = np.clip(3.0 * normalized - 2.0, 0, 1)
    red = np.clip(red + 0.15, 0, 1)
    green = np.clip(green + 0.18 * normalized, 0, 1)
    blue = np.clip(0.25 + 0.55 * (1.0 - normalized), 0, 1)
    return np.rint(np.dstack((red, green, blue)) * 255.0).astype(np.uint8)


def overlay_region(
    image: np.ndarray,
    region: np.ndarray,
    *,
    color: tuple[int, int, int] = (255, 30, 210),
    alpha: float = 0.35,
) -> np.ndarray:
    result = np.asarray(image, dtype=np.float64).copy()
    region = np.asarray(region, dtype=bool)
    result[region] = result[region] * (1.0 - alpha) + np.asarray(color, dtype=np.float64) * alpha
    boundary = inner_boundary(region)
    result[boundary] = np.asarray(color, dtype=np.float64)
    return np.clip(np.rint(result), 0, 255).astype(np.uint8)


def save_review_images(
    output_dir: str | Path,
    *,
    fused_u8: np.ndarray,
    updated_region: np.ndarray,
    boundary: np.ndarray,
    band: np.ndarray,
    seam_error_map: np.ndarray,
    gradient_error_map: np.ndarray,
) -> dict[str, str]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, str] = {}

    def save(name: str, image: np.ndarray) -> None:
        filename = f"{name}.png"
        write_png(destination / filename, image)
        outputs[name] = filename

    save("fused", fused_u8)
    save("updated_region", mask_png(updated_region))
    save("updated_region_overlay", overlay_region(fused_u8, updated_region))
    save("boundary_mask", mask_png(boundary))
    save("boundary_band", mask_png(band))
    save("seam_error_heatmap", heatmap_rgb(seam_error_map))
    save("gradient_error_heatmap", heatmap_rgb(gradient_error_map))
    return outputs
