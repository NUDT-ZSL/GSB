"""Per-part spectral peaks, tooth harmonics, and directional band attribution."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .diffusion import DiffusionResult
from .validation import InputBundle


SOURCE_SCREEN = "regular_screen_interference"
SOURCE_TOOTH = "tooth_period"
SOURCE_DIFFUSION = "error_diffusion_directional_bias"
SOURCE_UNCLASSIFIED = "unclassified"


def _local_maximum_2d(array: np.ndarray, radius: int) -> np.ndarray:
    maximum = array.copy()
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dy == 0 and dx == 0:
                continue
            shifted = np.full_like(array, -np.inf)
            ys = slice(max(0, dy), array.shape[0] + min(0, dy))
            yd = slice(max(0, -dy), array.shape[0] + min(0, -dy))
            xs = slice(max(0, dx), array.shape[1] + min(0, dx))
            xd = slice(max(0, -dx), array.shape[1] + min(0, -dx))
            shifted[ys, xs] = array[yd, xd]
            maximum = np.maximum(maximum, shifted)
    return maximum


def _classify_period(
    frequency_x: float,
    frequency_y: float,
    frequency: float,
    angle_deg: float,
    *,
    screen_pitch: tuple[float, float],
    tooth_frequencies: list[float],
    config: dict[str, Any],
) -> tuple[str, list[str]]:
    cfg = config["spectrum"]
    screen_tol = float(cfg["screen_frequency_tolerance"])
    tooth_tol = float(cfg["tooth_frequency_tolerance"])
    candidates: list[str] = []
    screen_fx = 1.0 / max(screen_pitch[0], 1e-9)
    screen_fy = 1.0 / max(screen_pitch[1], 1e-9)
    lattice_distance = min(
        math.hypot(frequency_x - ix * screen_fx, frequency_y - iy * screen_fy)
        for ix in range(-2, 3)
        for iy in range(-2, 3)
        if ix != 0 or iy != 0
    )
    screen_frequency = 0.5 * (screen_fx + screen_fy)
    if screen_frequency > 0 and lattice_distance / screen_frequency <= screen_tol:
        candidates.append(SOURCE_SCREEN)
    for tooth_frequency in tooth_frequencies:
        if tooth_frequency > 0 and abs(frequency - tooth_frequency) / tooth_frequency <= tooth_tol:
            candidates.append(SOURCE_TOOTH)
    if candidates:
        return candidates[0], candidates
    return SOURCE_UNCLASSIFIED, candidates


def _part_spectrum(
    binary: np.ndarray,
    mask: np.ndarray,
    label: int,
    bbox: list[int],
    *,
    screen_pitch: tuple[float, float],
    tooth_frequencies: list[float],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    x, y, width, height = bbox
    part = mask[y : y + height, x : x + width] == label
    if not np.any(part):
        return [], {"peak_count": 0, "reason": "empty_part"}
    values = binary[y : y + height, x : x + width].astype(np.float64)
    mean_value = float(values[part].mean())
    values = np.where(part, values, mean_value)
    values = values - float(values.mean())
    yy = np.hanning(values.shape[0])[:, None]
    xx = np.hanning(values.shape[1])[None, :]
    spectrum = np.fft.fftshift(np.fft.fft2(values * (yy * xx)))
    power = np.abs(spectrum) ** 2
    fy = np.fft.fftshift(np.fft.fftfreq(values.shape[0]))
    fx = np.fft.fftshift(np.fft.fftfreq(values.shape[1]))
    fy_grid, fx_grid = np.meshgrid(fy, fx, indexing="ij")
    radial = np.hypot(fy_grid, fx_grid)
    cfg = config["spectrum"]
    exclusion = float(cfg["peak_exclusion_radius_px"])
    valid = (
        (radial >= float(cfg["min_frequency"]))
        & (radial <= float(cfg["max_frequency"]))
        & ((fy_grid > 0.0) | ((fy_grid == 0.0) & (fx_grid > 0.0)))
        & (np.abs(fy_grid) > exclusion / max(values.shape[0], 1))
        & (np.abs(fx_grid) > exclusion / max(values.shape[1], 1))
    )
    # The upper-left/upper-right low-frequency corner is excluded explicitly.
    localized = _local_maximum_2d(power, 2)
    valid_power = power[valid]
    if valid_power.size == 0:
        return [], {"peak_count": 0, "reason": "empty_spectrum"}
    median_power = float(np.median(valid_power))
    maxima_mask = valid & (power >= localized) & (power > 0)
    peak_indices = np.argwhere(maxima_mask)
    if peak_indices.size == 0:
        return [], {
            "peak_count": 0,
            "median_power": median_power,
            "reason": "no_local_maximum",
        }
    scores = power[maxima_mask]
    order = np.argsort(scores)[::-1]
    peaks: list[dict[str, Any]] = []
    accepted_positions: list[tuple[int, int]] = []
    min_ratio = float(cfg["min_peak_ratio"])
    for position_index in order:
        iy, ix = peak_indices[position_index]
        if any((iy - py) ** 2 + (ix - px) ** 2 < 3**2 for py, px in accepted_positions):
            continue
        peak_power = float(power[iy, ix])
        ratio = peak_power / max(median_power, np.finfo(np.float64).tiny)
        if ratio < min_ratio:
            continue
        frequency_y = float(fy[iy])
        frequency_x = float(fx[ix])
        radial_frequency = float(math.hypot(frequency_y, frequency_x))
        angle = math.degrees(math.atan2(frequency_y, frequency_x)) % 360.0
        source, candidates = _classify_period(
            frequency_x,
            frequency_y,
            radial_frequency,
            angle,
            screen_pitch=screen_pitch,
            tooth_frequencies=tooth_frequencies,
            config=config,
        )
        accepted_positions.append((iy, ix))
        peaks.append(
            {
                "frequency_cycles_per_px": [frequency_x, frequency_y],
                "radial_frequency": radial_frequency,
                "period_px": float(1.0 / radial_frequency) if radial_frequency > 0 else None,
                "angle_deg": float(angle),
                "power": peak_power,
                "peak_to_median_ratio": float(ratio),
                "source": source,
                "candidate_sources": candidates,
            }
        )
        if len(peaks) >= int(cfg["max_peaks"]):
            break
    return peaks, {
        "peak_count": len(peaks),
        "median_power": median_power,
        "roi_wh": [int(values.shape[1]), int(values.shape[0])],
    }


def _moving_average(profile: np.ndarray, window: int) -> np.ndarray:
    window = max(1, min(int(window), profile.size))
    if window <= 1:
        return profile.copy()
    kernel = np.ones(window, dtype=np.float64) / window
    padded = np.pad(profile, (window // 2, window - 1 - window // 2), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def _autocorrelation_peak(profile: np.ndarray, max_lag: int) -> tuple[float, int, float]:
    centered = profile - float(profile.mean())
    variance = float(np.dot(centered, centered))
    if variance <= 1e-12:
        return 0.0, 0, 0.0
    size = 1
    while size < 2 * centered.size:
        size <<= 1
    fft_values = np.fft.rfft(centered, n=size)
    ac = np.fft.irfft(np.abs(fft_values) ** 2, n=size)[: centered.size]
    ac = ac / ac[0]
    max_lag = max(1, min(max_lag, ac.size - 2))
    if max_lag < 2:
        return 0.0, 0, 0.0
    best_lag = 1
    best_value = -np.inf
    for lag in range(2, max_lag + 1):
        if ac[lag] > ac[lag - 1] and (lag + 1 >= ac.size or ac[lag] >= ac[lag + 1]):
            if ac[lag] > best_value:
                best_value = float(ac[lag])
                best_lag = lag
    return best_value, best_lag, variance


def _profile_bands(
    profile: np.ndarray,
    *,
    axis: str,
    offset: int,
    z_threshold: float,
    max_bands: int,
    screen_pitch: float,
    tooth_periods: list[float],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    smooth_window = max(3, int(round(screen_pitch * 3)))
    residual = profile - _moving_average(profile, smooth_window)
    scale = float(np.std(residual))
    if scale <= 1e-12:
        return []
    zscore = residual / scale
    max_lag = max(4, min(profile.size // 2, int(round(max(tooth_periods or [1.0]) * 3)) + 8))
    ac_peak, ac_lag, _ = _autocorrelation_peak(residual, max_lag)
    source = SOURCE_UNCLASSIFIED
    reason = "no_periodic_autocorrelation_peak"
    screen_tolerance = float(config["spectrum"]["screen_frequency_tolerance"])
    tooth_tolerance = float(config["spectrum"]["tooth_frequency_tolerance"])
    if ac_peak >= 0.22 and abs(ac_lag - screen_pitch) / max(screen_pitch, 1e-6) <= screen_tolerance:
        source = SOURCE_SCREEN
        reason = "autocorrelation_lag_matches_screen_pitch"
    elif ac_peak >= 0.20 and any(
        abs(ac_lag - period) / max(period, 1e-6) <= tooth_tolerance for period in tooth_periods
    ):
        source = SOURCE_TOOTH
        reason = "autocorrelation_lag_matches_tooth_period"
    elif ac_peak >= 0.20:
        source = SOURCE_DIFFUSION
        reason = "periodic_profile_not_explained_by_screen_or_tooth"
    elif axis == "rows":
        source = SOURCE_DIFFUSION
        reason = "scan_row_band_without_expected_period"

    mask = zscore > z_threshold
    bands: list[dict[str, Any]] = []
    start: int | None = None
    for index in range(mask.size + 1):
        inside = index < mask.size and bool(mask[index])
        if inside and start is None:
            start = index
        elif not inside and start is not None:
            end = index - 1
            if end - start + 1 >= 2:
                bands.append(
                    {
                        "axis": axis,
                        "range": [int(offset + start), int(offset + end)],
                        "length": int(end - start + 1),
                        "mean_z": float(np.mean(zscore[start : end + 1])),
                        "max_z": float(np.max(zscore[start : end + 1])),
                        "source": source,
                        "reason": reason,
                        "autocorrelation_peak": float(ac_peak),
                        "autocorrelation_lag_px": int(ac_lag),
                    }
                )
            start = None
        if len(bands) >= max_bands:
            break
    return bands


def _angular_tooth_check(
    binary: np.ndarray,
    mask: np.ndarray,
    region: dict[str, Any],
) -> dict[str, Any]:
    center = region.get("center")
    if not isinstance(center, list) or len(center) != 2:
        return {"id": region.get("id"), "skipped": True, "reason": "no_center"}
    cx, cy = int(center[0]), int(center[1])
    root = float(region.get("root_radius_px", 0.0))
    tip = float(region.get("tip_radius_px", 0.0))
    expected = int(region.get("tooth_count", 0))
    if tip <= root or expected <= 0:
        return {"id": region.get("id"), "skipped": True, "reason": "no_tooth_geometry"}
    radius = 0.5 * (root + tip)
    angle_count = 720
    angles = np.linspace(0.0, 2.0 * np.pi, angle_count, endpoint=False)
    xs = np.rint(cx + radius * np.cos(angles)).astype(np.int64)
    ys = np.rint(cy + radius * np.sin(angles)).astype(np.int64)
    valid = (xs >= 0) & (xs < binary.shape[1]) & (ys >= 0) & (ys < binary.shape[0])
    if not np.any(valid):
        return {"id": region.get("id"), "skipped": True, "reason": "ring_outside_image"}
    values = binary[ys[valid], xs[valid]].astype(np.float64)
    profile = values - values.mean()
    fft_values = np.abs(np.fft.rfft(profile)) ** 2
    fft_values[0] = 0.0
    if fft_values.size <= 1:
        return {"id": region.get("id"), "skipped": True, "reason": "empty_angular_profile"}
    detected_bin = int(np.argmax(fft_values))
    denominator = float(np.sum(fft_values))
    strength = float(fft_values[detected_bin] / denominator) if denominator > 0 else 0.0
    period_px = float(2.0 * math.pi * radius / detected_bin) if detected_bin > 0 else None
    return {
        "id": region.get("id"),
        "center": [cx, cy],
        "sample_radius_px": float(radius),
        "expected_tooth_count": expected,
        "expected_period_px": float(2.0 * math.pi * radius / expected),
        "detected_harmonic": detected_bin,
        "detected_period_px": period_px,
        "harmonic_strength": strength,
        "matches_expected": bool(detected_bin == expected),
        "sample_count": int(values.size),
    }


def build_uniformity_report(
    bundle: InputBundle,
    final: DiffusionResult,
) -> dict[str, Any]:
    cfg = bundle.config["spectrum"]
    cell_x, cell_y = bundle.print_grid["screen_pitch_px"]
    screen_pitch = 0.5 * (cell_x + cell_y)
    screen_frequency = 1.0 / screen_pitch
    z_threshold = float(cfg["band_z_threshold"])
    max_bands = int(cfg["max_bands"])
    parts: list[dict[str, Any]] = []
    overall_ok = True
    for label, spec in sorted(bundle.part_specs.items()):
        bbox = bundle.bbox_for_label(label)
        if bbox is None:
            continue
        regions = [r for r in bundle.detail_regions if int(r["part_label"]) == label]
        tooth_regions = [r for r in regions if r.get("kind") == "tooth"]
        tooth_periods = [
            float(r["tooth_period_px"])
            for r in tooth_regions
            if isinstance(r.get("tooth_period_px"), (int, float))
        ]
        peaks, spectrum_info = _part_spectrum(
            final.binary,
            bundle.mask,
            label,
            bbox,
            screen_pitch=(cell_x, cell_y),
            tooth_frequencies=[1.0 / period for period in tooth_periods if period > 0],
            config=bundle.config,
        )
        tooth_checks = [
            _angular_tooth_check(final.binary, bundle.mask, region)
            for region in tooth_regions
        ]

        part_mask = bundle.mask == label
        x, y, width, height = bbox
        part_roi = part_mask[y : y + height, x : x + width]
        binary_roi = final.binary[y : y + height, x : x + width].astype(np.float64)
        row_profile = np.array(
            [binary_roi[row, part_roi[row]].mean() if np.any(part_roi[row]) else 0.0 for row in range(height)]
        )
        column_profile = np.array(
            [binary_roi[part_roi[:, col], col].mean() if np.any(part_roi[:, col]) else 0.0 for col in range(width)]
        )
        bands = _profile_bands(
            row_profile,
            axis="rows",
            offset=y,
            z_threshold=z_threshold,
            max_bands=max_bands,
            screen_pitch=cell_y,
            tooth_periods=tooth_periods,
            config=bundle.config,
        )
        bands.extend(
            _profile_bands(
                column_profile,
                axis="columns",
                offset=x,
                z_threshold=z_threshold,
                max_bands=max_bands,
                screen_pitch=cell_x,
                tooth_periods=tooth_periods,
                config=bundle.config,
            )
        )

        source_counts = {
            SOURCE_SCREEN: 0,
            SOURCE_TOOTH: 0,
            SOURCE_DIFFUSION: 0,
            SOURCE_UNCLASSIFIED: 0,
        }
        for peak in peaks:
            source_counts[str(peak["source"])] += 1
        for band in bands:
            source_counts[str(band["source"])] += 1
        strong_moire = [
            peak
            for peak in peaks
            if peak["source"] == SOURCE_SCREEN and peak["peak_to_median_ratio"] >= float(cfg["min_peak_ratio"])
        ]
        classified_peaks = [peak for peak in peaks if peak["source"] != SOURCE_UNCLASSIFIED]
        classified_bands = [band for band in bands if band["source"] != SOURCE_UNCLASSIFIED]
        risk_score = (
            2.0 * len(strong_moire)
            + len([band for band in classified_bands if band["source"] == SOURCE_TOOTH])
            + 0.5 * len([band for band in classified_bands if band["source"] == SOURCE_DIFFUSION])
        )
        uniformity_ok = not (
            len(strong_moire) >= int(cfg["max_strong_screen_peaks_before_fail"])
            or risk_score >= float(cfg["max_risk_score_before_fail"])
        )
        overall_ok = overall_ok and uniformity_ok
        parts.append(
            {
                "label": label,
                "name": spec.get("name"),
                "bbox_xywh": bbox,
                "spectrum_info": spectrum_info,
                "screen_frequency_cycles_per_px": screen_frequency,
                "screen_pitch_px": screen_pitch,
                "tooth_expected_frequencies": [
                    {"period_px": period, "frequency": 1.0 / period}
                    for period in tooth_periods
                    if period > 0
                ],
                "spectrum_peaks": peaks,
                "moire_peaks": classified_peaks,
                "angular_tooth_checks": tooth_checks,
                "row_profile_variance": float(np.var(row_profile)),
                "column_profile_variance": float(np.var(column_profile)),
                "directionality_ratio_row_over_column": float(
                    np.var(row_profile) / max(np.var(column_profile), 1e-12)
                ),
                "bands": bands,
                "source_counts": source_counts,
                "uniformity": {
                    "passed": bool(uniformity_ok),
                    "risk_score": float(risk_score),
                    "regular_screen_moire_peak_count": len(strong_moire),
                    "classified_band_count": len(classified_bands),
                    "reasons": (
                        []
                        if uniformity_ok
                        else [
                            "strong_screen_moire_peaks",
                            "periodic_bands_detected",
                        ]
                    ),
                },
                "truth_check": {
                    "all_tooth_harmonics_match": all(
                        check.get("matches_expected", False)
                        for check in tooth_checks
                        if not check.get("skipped")
                    )
                    if any(not check.get("skipped") for check in tooth_checks)
                    else None,
                    "angular_checks": tooth_checks,
                },
            }
        )

    return {
        "schema": "clock-halftone-uniformity-report/v1",
        "ok": bool(overall_ok),
        "attribution_classes": [
            SOURCE_SCREEN,
            SOURCE_TOOTH,
            SOURCE_DIFFUSION,
            SOURCE_UNCLASSIFIED,
        ],
        "parts": parts,
    }
