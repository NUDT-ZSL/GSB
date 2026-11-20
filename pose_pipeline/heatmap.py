from __future__ import annotations

import math
from typing import Any

import numpy as np

from .utils import clamp, round_float


EXACT_OFFSET_RULE = (
    "1D quadratic interpolation on the peak pixel and its two horizontal/vertical "
    "neighbors; no interpolation at canvas borders (offset 0); offsets clamped to "
    "[-0.5, 0.5] and final coordinates clipped to [0, W-1] x [0, H-1]"
)

APPROX_OFFSET_RULE = (
    "max-pooled coarse bins; peak coordinate is the center of the winning bin; "
    "subpixel offset is fixed at (0, 0)"
)


def _quadratic_offset(left: float, center: float, right: float) -> float:
    denominator = left - 2.0 * center + right
    if abs(denominator) < 1e-12:
        return 0.0
    return clamp(0.5 * (left - right) / denominator, -0.5, 0.5)


def _local_maxima(image: np.ndarray, min_intensity: float) -> list[tuple[int, int]]:
    height, width = image.shape
    padded = np.pad(image, 1, mode="constant", constant_values=-np.inf)
    center = padded[1:-1, 1:-1]
    is_max = np.ones((height, width), dtype=bool)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            neighbor = padded[1 + dy : 1 + dy + height, 1 + dx : 1 + dx + width]
            is_max &= center > neighbor
    is_max &= image >= float(min_intensity)
    return [(int(y), int(x)) for y, x in np.argwhere(is_max)]


def _max_pool(image: np.ndarray, block_size: int) -> np.ndarray:
    height, width = image.shape
    padded_height = int(math.ceil(height / block_size)) * block_size
    padded_width = int(math.ceil(width / block_size)) * block_size
    padded = np.full((padded_height, padded_width), -np.inf, dtype=np.float32)
    padded[:height, :width] = image
    blocks = padded.reshape(
        padded_height // block_size,
        block_size,
        padded_width // block_size,
        block_size,
    )
    return blocks.max(axis=(1, 3))


def _peak_record(
    sample: dict[str, Any],
    channel: int,
    rank: int,
    x_pixel: float,
    y_pixel: float,
    x: float,
    y: float,
    intensity: float,
    dx: float,
    dy: float,
    path: str,
    quantization_px: float,
) -> dict[str, Any]:
    num_keypoints = sample["num_keypoints"]
    skeleton_index = channel // num_keypoints
    keypoint_index = channel % num_keypoints
    skeleton = sample["skeletons"][skeleton_index]
    return {
        "peak_id": f"{sample['image_id']}:ch{channel:03d}:p{rank}",
        "channel_index": channel,
        "skeleton_id": skeleton_index,
        "source": skeleton["source"],
        "keypoint_index": keypoint_index,
        "rank": rank,
        "path": path,
        "x_pixel": int(round(x_pixel)),
        "y_pixel": int(round(y_pixel)),
        "x": round_float(x),
        "y": round_float(y),
        "intensity": round_float(intensity),
        "subpixel_offset": {
            "dx": round_float(dx),
            "dy": round_float(dy),
        },
        "quantization_px": round_float(quantization_px),
    }


def _sort_peaks(peaks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        peaks,
        key=lambda peak: (
            -peak["intensity"],
            peak["keypoint_index"],
            peak["y_pixel"],
            peak["x_pixel"],
            peak["channel_index"],
            peak["rank"],
        ),
    )


def decode_exact(
    heatmaps: np.ndarray,
    sample: dict[str, Any],
    heatmap_config: dict[str, Any],
) -> dict[str, Any]:
    target = heatmaps[1]
    channels, height, width = target.shape
    max_peaks = int(heatmap_config["max_peaks_per_channel"])
    min_intensity = float(heatmap_config["min_peak_intensity"])
    peaks: list[dict[str, Any]] = []

    for channel in range(channels):
        image = target[channel]
        candidates = _local_maxima(image, min_intensity)
        candidates.sort(key=lambda item: (-float(image[item]), item[0], item[1]))
        for rank, (y_pixel, x_pixel) in enumerate(candidates[:max_peaks]):
            dx = 0.0
            dy = 0.0
            if 0 < x_pixel < width - 1:
                dx = _quadratic_offset(
                    float(image[y_pixel, x_pixel - 1]),
                    float(image[y_pixel, x_pixel]),
                    float(image[y_pixel, x_pixel + 1]),
                )
            if 0 < y_pixel < height - 1:
                dy = _quadratic_offset(
                    float(image[y_pixel - 1, x_pixel]),
                    float(image[y_pixel, x_pixel]),
                    float(image[y_pixel + 1, x_pixel]),
                )
            x = min(width - 1.0, max(0.0, x_pixel + dx))
            y = min(height - 1.0, max(0.0, y_pixel + dy))
            peaks.append(
                _peak_record(
                    sample=sample,
                    channel=channel,
                    rank=rank,
                    x_pixel=x_pixel,
                    y_pixel=y_pixel,
                    x=x,
                    y=y,
                    intensity=float(image[y_pixel, x_pixel]),
                    dx=x - x_pixel,
                    dy=y - y_pixel,
                    path="exact",
                    quantization_px=0.0,
                )
            )

    comparison_count = channels * height * width * 8
    return {
        "path": "exact",
        "interpolation": "1d_quadratic",
        "offset_rule": EXACT_OFFSET_RULE,
        "boundary_rule": "offset_zero_then_clip_to_canvas",
        "peaks": _sort_peaks(peaks),
        "comparison_count": comparison_count,
        "refinement_count": len(peaks) * 4,
        "channel_count": channels,
    }


def decode_approx(
    heatmaps: np.ndarray,
    sample: dict[str, Any],
    heatmap_config: dict[str, Any],
) -> dict[str, Any]:
    target = heatmaps[1]
    channels, height, width = target.shape
    block_size = int(heatmap_config["approx_block_size"])
    quantization_px = float(heatmap_config["approx_quantization_px"])
    max_peaks = int(heatmap_config["max_peaks_per_channel"])
    min_intensity = float(heatmap_config["min_peak_intensity"])
    peaks: list[dict[str, Any]] = []
    coarse_height = int(math.ceil(height / block_size))
    coarse_width = int(math.ceil(width / block_size))

    for channel in range(channels):
        coarse = _max_pool(target[channel], block_size)
        candidates = _local_maxima(coarse, min_intensity)
        candidates.sort(key=lambda item: (-float(coarse[item]), item[0], item[1]))
        for rank, (coarse_y, coarse_x) in enumerate(candidates[:max_peaks]):
            x_start = coarse_x * block_size
            y_start = coarse_y * block_size
            x_span = min(block_size, width - x_start)
            y_span = min(block_size, height - y_start)
            x = x_start + (x_span - 1) / 2.0
            y = y_start + (y_span - 1) / 2.0
            peaks.append(
                _peak_record(
                    sample=sample,
                    channel=channel,
                    rank=rank,
                    x_pixel=x,
                    y_pixel=y,
                    x=x,
                    y=y,
                    intensity=float(coarse[coarse_y, coarse_x]),
                    dx=0.0,
                    dy=0.0,
                    path="approx",
                    quantization_px=quantization_px,
                )
            )

    comparison_count = channels * height * width + channels * coarse_height * coarse_width * 8
    return {
        "path": "approx",
        "interpolation": "none",
        "offset_rule": APPROX_OFFSET_RULE,
        "boundary_rule": "coarse_bin_center_clipped_by_bin_span",
        "peaks": _sort_peaks(peaks),
        "comparison_count": comparison_count,
        "refinement_count": 0,
        "channel_count": channels,
        "block_size": block_size,
        "coarse_shape": [coarse_height, coarse_width],
    }


def decode_heatmaps(
    heatmaps: np.ndarray,
    sample: dict[str, Any],
    path: str,
    heatmap_config: dict[str, Any],
) -> dict[str, Any]:
    if path == "exact":
        return decode_exact(heatmaps, sample, heatmap_config)
    if path == "approx":
        return decode_approx(heatmaps, sample, heatmap_config)
    raise ValueError(f"unsupported peak path: {path}")


def selected_peaks_by_channel(decoded: dict[str, Any]) -> dict[int, dict[str, Any]]:
    selected: dict[int, dict[str, Any]] = {}
    for peak in decoded["peaks"]:
        channel = peak["channel_index"]
        if channel in selected:
            continue
        selected[channel] = peak
    return selected
