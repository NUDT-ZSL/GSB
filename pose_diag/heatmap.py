"""Deterministic heatmap peak detection and sub-pixel refinement."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import PipelineConfig
from .errors import BusinessError
from .io_utils import canonical_json_bytes, round_float, sha256_bytes
from .models import Peak, RegionSpec


@dataclass(frozen=True)
class RawPeak:
    channel: int
    y: int
    x: int
    intensity: float

    @property
    def sort_key(self) -> tuple[float, int, int, int]:
        return (-self.intensity, self.channel, self.y, self.x)


def validate_heatmap_channels(
    heatmaps: np.ndarray,
    expected_channels: int,
    scene_id: str | None = None,
) -> None:
    actual = int(heatmaps.shape[0]) if heatmaps.ndim == 3 else 0
    if heatmaps.ndim != 3 or actual != expected_channels:
        raise BusinessError(
            "E_CHANNEL_MISMATCH",
            "heatmap channel count does not match num_keypoints",
            field="num_keypoints",
            scene_id=scene_id,
            details={
                "heatmap_channels": actual,
                "num_keypoints": expected_channels,
            },
        )


def detect_peaks(
    heatmaps: np.ndarray,
    expected_channels: int,
    threshold: float,
    neighborhood_radius: int,
    *,
    offset: tuple[int, int] = (0, 0),
    exclude_edge_margin: int = 0,
    scene_id: str | None = None,
) -> list[RawPeak]:
    """Detect 3x3 local maxima with explicit row-major plateau tie-breaking."""

    validate_heatmap_channels(heatmaps, expected_channels, scene_id)
    values = np.asarray(heatmaps, dtype=np.float64)
    channels, height, width = values.shape
    radius = int(neighborhood_radius)
    raw: list[RawPeak] = []
    x_offset, y_offset = offset

    for channel in range(channels):
        plane = values[channel]
        candidates = plane > float(threshold)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if dy == 0 and dx == 0:
                    continue
                shifted = np.full_like(plane, -np.inf)
                src_y0 = max(0, -dy)
                src_y1 = min(height, height - dy)
                src_x0 = max(0, -dx)
                src_x1 = min(width, width - dx)
                dst_y0 = src_y0 + dy
                dst_y1 = src_y1 + dy
                dst_x0 = src_x0 + dx
                dst_x1 = src_x1 + dx
                shifted[dst_y0:dst_y1, dst_x0:dst_x1] = plane[
                    src_y0:src_y1, src_x0:src_x1
                ]
                candidates &= plane >= shifted
                # Equal values are accepted only by the first pixel in
                # (row, column) order. This makes plateaus deterministic.
                earlier = dy < 0 or (dy == 0 and dx < 0)
                if earlier:
                    candidates &= plane > shifted

        if exclude_edge_margin > 0:
            margin = int(exclude_edge_margin)
            if margin * 2 >= height or margin * 2 >= width:
                ys: Iterable[int] = ()
                xs: Iterable[int] = ()
            else:
                candidates[:margin, :] = False
                candidates[-margin:, :] = False
                candidates[:, :margin] = False
                candidates[:, -margin:] = False

        ys, xs = np.nonzero(candidates)
        for y, x in zip(ys.tolist(), xs.tolist(), strict=True):
            raw.append(
                RawPeak(
                    channel=channel,
                    y=int(y) + y_offset,
                    x=int(x) + x_offset,
                    intensity=float(plane[y, x]),
                )
            )

    raw.sort(key=lambda peak: peak.sort_key)
    return raw


def refine_peaks(
    raw_peaks: Iterable[RawPeak],
    heatmaps: np.ndarray,
    config: PipelineConfig,
    *,
    offset: tuple[int, int] = (0, 0),
    scene_id: str | None = None,
) -> list[Peak]:
    if not 0 <= config.quantization_decimals <= 6:
        raise BusinessError(
            "E_QUANT_INVALID",
            "quantization_decimals must be between 0 and 6",
            field="quantization_decimals",
            scene_id=scene_id,
            details={"value": config.quantization_decimals},
        )
    values = np.asarray(heatmaps, dtype=np.float64)
    x_offset, y_offset = offset
    radius = int(config.refine_radius)
    scale = 10.0 ** config.quantization_decimals
    refined: list[Peak] = []

    for raw in raw_peaks:
        local_y = raw.y - y_offset
        local_x = raw.x - x_offset
        y0 = max(0, local_y - radius)
        y1 = min(values.shape[1], local_y + radius + 1)
        x0 = max(0, local_x - radius)
        x1 = min(values.shape[2], local_x + radius + 1)
        clipped = (
            local_y - radius < 0
            or local_y + radius >= values.shape[1]
            or local_x - radius < 0
            or local_x + radius >= values.shape[2]
        )
        patch = values[raw.channel, y0:y1, x0:x1]
        weights = patch - float(np.min(patch))
        weight_sum = float(np.sum(weights, dtype=np.float64))
        if not np.isfinite(weight_sum) or weight_sum <= 1e-15:
            refined_y = float(raw.y)
            refined_x = float(raw.x)
        else:
            py = np.arange(y0, y1, dtype=np.float64)
            px = np.arange(x0, x1, dtype=np.float64)
            centroid_y = float(np.sum(weights * py[:, None], dtype=np.float64) / weight_sum)
            centroid_x = float(np.sum(weights * px[None, :], dtype=np.float64) / weight_sum)
            refined_y = y_offset + centroid_y
            refined_x = x_offset + centroid_x

        quantized_y = float(np.floor(refined_y * scale + 0.5) / scale)
        quantized_x = float(np.floor(refined_x * scale + 0.5) / scale)
        refined.append(
            Peak(
                channel=raw.channel,
                y=raw.y,
                x=raw.x,
                intensity=raw.intensity,
                refined_y=round_float(quantized_y, config.quantization_decimals),
                refined_x=round_float(quantized_x, config.quantization_decimals),
                window_clipped=bool(clipped),
            )
        )
    refined.sort(key=lambda peak: peak.sort_key)
    return refined


def detect_and_refine(
    heatmaps: np.ndarray,
    expected_channels: int,
    config: PipelineConfig,
    *,
    offset: tuple[int, int] = (0, 0),
    exclude_edge_margin: int = 0,
    scene_id: str | None = None,
) -> list[Peak]:
    raw = detect_peaks(
        heatmaps,
        expected_channels,
        config.peak_threshold,
        config.peak_neighborhood_radius,
        offset=offset,
        exclude_edge_margin=exclude_edge_margin,
        scene_id=scene_id,
    )
    return refine_peaks(raw, heatmaps, config, offset=offset, scene_id=scene_id)


def peaks_bytes(peaks: Iterable[Peak]) -> bytes:
    return canonical_json_bytes([peak.canonical() for peak in peaks])


def peaks_digest(peaks: Iterable[Peak]) -> str:
    return sha256_bytes(peaks_bytes(peaks))


def filter_peaks_to_region(peaks: Iterable[Peak], region: RegionSpec) -> list[Peak]:
    return [
        peak for peak in peaks if region.contains(float(peak.x), float(peak.y))
    ]


def compare_peak_sets(
    full_peaks: Iterable[Peak],
    preview_peaks: Iterable[Peak],
    region: RegionSpec,
    *,
    scene_id: str | None = None,
) -> dict[str, Any]:
    """Compare peaks whose raw centers lie in the region core."""

    full = sorted(
        filter_peaks_to_region(full_peaks, region),
        key=lambda peak: (peak.channel, peak.y, peak.x),
    )
    preview = sorted(
        filter_peaks_to_region(preview_peaks, region),
        key=lambda peak: (peak.channel, peak.y, peak.x),
    )
    full_payload = [peak.canonical() for peak in full]
    preview_payload = [peak.canonical() for peak in preview]
    if canonical_json_bytes(full_payload) == canonical_json_bytes(preview_payload):
        return {
            "business_code": "OK",
            "consistent": True,
            "region_id": region.region_id,
            "peak_count": len(full),
        }

    full_keys = {(peak.channel, peak.y, peak.x): peak for peak in full}
    preview_keys = {(peak.channel, peak.y, peak.x): peak for peak in preview}
    first_key: tuple[int, int, int] | None = None
    for key in sorted(set(full_keys) | set(preview_keys)):
        if key not in full_keys:
            first_key = key
            break
        if key not in preview_keys:
            first_key = key
            break
        if full_keys[key].canonical() != preview_keys[key].canonical():
            first_key = key
            break
    assert first_key is not None
    full_peak = full_keys.get(first_key)
    preview_peak = preview_keys.get(first_key)
    return {
        "business_code": "E_PEAK_MISMATCH",
        "consistent": False,
        "region_id": region.region_id,
        "scene_id": scene_id,
        "first_deviation": {
            "channel": first_key[0],
            "y": first_key[1],
            "x": first_key[2],
            "full": full_peak.canonical() if full_peak else None,
            "preview": preview_peak.canonical() if preview_peak else None,
        },
    }

