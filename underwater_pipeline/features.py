"""Per-frame colour and edge features used for shot boundary detection."""

from __future__ import annotations

import numpy as np


def validate_frame_array(frames: np.ndarray) -> np.ndarray:
    """Return a C-contiguous integer RGB frame array or raise ``ValueError``."""

    array = np.asarray(frames)
    if array.ndim != 4:
        raise ValueError("frames must have shape (n, height, width, channels)")
    if array.shape[3] not in (1, 3):
        raise ValueError("frames must have one or three channels")
    if array.dtype.kind not in "iu":
        raise TypeError("frames must contain integer pixel values")
    return np.ascontiguousarray(array)


def rgb_to_float(frames: np.ndarray) -> np.ndarray:
    array = validate_frame_array(frames)
    if np.issubdtype(array.dtype, np.signedinteger):
        minimum = int(array.min()) if array.size else 0
        maximum = int(array.max()) if array.size else 0
        if minimum < 0 or maximum > 255:
            raise ValueError("integer frames must lie in [0, 255]")
    elif array.size and int(array.max()) > 255:
        raise ValueError("integer frames must lie in [0, 255]")
    return array.astype(np.float64) / 255.0


def luminance(frames: np.ndarray) -> np.ndarray:
    rgb = rgb_to_float(frames)
    if rgb.shape[3] == 1:
        return rgb[:, :, :, 0]
    return (
        0.2126 * rgb[:, :, :, 0]
        + 0.7152 * rgb[:, :, :, 1]
        + 0.0722 * rgb[:, :, :, 2]
    )


def color_histograms(frames: np.ndarray, bins: int = 16) -> np.ndarray:
    """Return concatenated, L1-normalised per-channel histograms."""

    rgb = rgb_to_float(frames)
    count = rgb.shape[0]
    channels = rgb.shape[3]
    quantised = np.minimum((rgb * bins).astype(np.int16), bins - 1)
    histograms = np.zeros((count, channels, bins), dtype=np.float64)
    for frame_index in range(count):
        for channel in range(channels):
            histograms[frame_index, channel] = np.bincount(
                quantised[frame_index, :, :, channel].ravel(),
                minlength=bins,
            )[:bins]
    totals = histograms.sum(axis=2, keepdims=True)
    histograms /= np.maximum(totals, 1.0)
    return histograms.reshape(count, channels * bins)


def histogram_distance(left: np.ndarray, right: np.ndarray) -> float:
    if left.size != right.size:
        raise ValueError("histogram sizes differ")
    channel_count = 3 if left.size % 3 == 0 else 1
    bins = left.size // channel_count
    lhs = left.reshape(channel_count, bins)
    rhs = right.reshape(channel_count, bins)
    return float(0.5 * np.abs(lhs - rhs).sum(axis=1).mean())


def edge_magnitude(frames: np.ndarray) -> np.ndarray:
    gray = luminance(frames)
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, :, 1:-1] = 0.5 * (gray[:, :, 2:] - gray[:, :, :-2])
    gy[:, 1:-1, :] = 0.5 * (gray[:, 2:, :] - gray[:, :-2, :])
    return np.sqrt(gx * gx + gy * gy)


def edge_densities(frames: np.ndarray, threshold: float = 0.075) -> np.ndarray:
    magnitudes = edge_magnitude(frames)
    return (magnitudes >= threshold).mean(axis=(1, 2))


def frame_boundary_metrics(
    frames: np.ndarray,
    config,
) -> dict[str, np.ndarray]:
    """Compute colour-histogram and edge-density terms for every boundary."""

    count = frames.shape[0]
    histograms = color_histograms(frames, config.hist_bins)
    densities = edge_densities(frames, config.edge_threshold)
    hist_distance = np.zeros(count, dtype=np.float64)
    edge_change = np.zeros(count, dtype=np.float64)
    for index in range(1, count):
        hist_distance[index] = histogram_distance(
            histograms[index - 1], histograms[index]
        )
        edge_change[index] = abs(float(densities[index] - densities[index - 1]))
    edge_change_normalised = np.clip(
        edge_change / max(config.edge_change_scale, 1e-9), 0.0, 1.0
    )
    combined = np.clip(
        config.hist_weight * hist_distance
        + config.edge_weight * edge_change_normalised,
        0.0,
        1.0,
    )
    return {
        "hist_distance": hist_distance,
        "edge_density": densities,
        "edge_density_change": edge_change,
        "edge_change_normalised": edge_change_normalised,
        "boundary_metric": combined,
    }


def adaptive_boundary_thresholds(
    metrics: np.ndarray,
    config,
) -> np.ndarray:
    """Median/MAD threshold with a local median/MAD refinement."""

    count = int(metrics.shape[0])
    thresholds = np.full(count, np.inf, dtype=np.float64)
    if count <= 1:
        return thresholds

    values = metrics[1:]
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median))) * 1.4826
    global_threshold = median + max(config.adaptive_mad_k * mad, config.adaptive_floor)

    for index in range(1, count):
        low = max(1, index - config.adaptive_window)
        high = min(count, index + config.adaptive_window + 1)
        local = metrics[low:high]
        local_median = float(np.median(local))
        local_mad = float(np.median(np.abs(local - local_median))) * 1.4826
        local_threshold = local_median + max(
            config.adaptive_mad_k * local_mad, config.adaptive_floor
        )
        thresholds[index] = max(global_threshold, local_threshold)
    return thresholds


def find_raw_boundary_candidates(
    metrics: np.ndarray,
    thresholds: np.ndarray,
    config,
) -> list[dict]:
    candidates: list[dict] = []
    count = int(metrics.shape[0])
    radius = max(1, int(config.local_peak_radius))
    above = np.ones(count, dtype=bool)
    above &= metrics >= thresholds
    above[0] = False
    selected: set[int] = set()
    for run_start, run_end in _mask_runs(above):
        if run_end - run_start <= 3:
            # Keep both sides of a one-frame flash/occlusion. A single local-max
            # rule would silently turn the isolated frame into a permanent cut.
            selected.update(range(run_start, run_end))
        else:
            for index in range(run_start, run_end):
                low = max(1, index - radius)
                high = min(count, index + radius + 1)
                if metrics[index] >= metrics[low:high].max():
                    selected.add(index)

    for index in sorted(selected):
        value = float(metrics[index])
        candidates.append(
            {
                "frame_index": index,
                "boundary_metric": value,
                "adaptive_threshold": float(thresholds[index]),
                "strength": value / max(float(thresholds[index]), 1e-9),
            }
        )
    return candidates


def _mask_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    index = 0
    while index < mask.size:
        if not mask[index]:
            index += 1
            continue
        start = index
        while index < mask.size and mask[index]:
            index += 1
        runs.append((start, index))
    return runs
