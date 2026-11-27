"""Connectivity and edge-fidelity checks in annotated detail regions."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .diffusion import DiffusionResult
from .validation import InputBundle


def _box_mean(array: np.ndarray, size: int) -> np.ndarray:
    size = max(1, int(size))
    if size <= 1:
        return array.astype(np.float64, copy=True)
    radius = size // 2
    padded = np.pad(array.astype(np.float64), radius, mode="edge")
    integral = np.pad(padded, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    sums = (
        integral[size:, size:]
        - integral[:-size, size:]
        - integral[size:, :-size]
        + integral[:-size, :-size]
    )
    return sums / float(size * size)


def _odd_smoothing_size(bundle: InputBundle) -> int:
    cfg = bundle.config["detail"]
    cell_pitch = 0.5 * sum(bundle.print_grid["screen_pitch_px"])
    dot_px = float(bundle.print_grid["ink_dot_diameter_px"])
    raw = max(
        float(cfg["min_smoothing_px"]),
        cell_pitch * float(cfg["smoothing_cell_factor"]),
        dot_px * float(cfg["smoothing_dot_factor"]),
    )
    size = int(round(raw))
    if size % 2 == 0:
        size += 1
    return max(1, min(int(cfg["max_smoothing_px"]), size))


def _edge_points(mask: np.ndarray) -> np.ndarray:
    boundary = np.zeros(mask.shape, dtype=bool)
    boundary[:-1, :] |= mask[:-1, :] != mask[1:, :]
    boundary[:, :-1] |= mask[:, :-1] != mask[:, 1:]
    boundary[1:, :] |= mask[1:, :] != mask[:-1, :]
    boundary[:, 1:] |= mask[:, 1:] != mask[:, :-1]
    return boundary


def _fill_small_holes(solid: np.ndarray, max_area: int) -> tuple[np.ndarray, int]:
    """Fill enclosed background holes no larger than max_area pixels."""
    height, width = solid.shape
    visited = np.zeros((height, width), dtype=bool)
    filled = solid.copy()
    filled_count = 0
    for y0 in range(height):
        for x0 in range(width):
            if solid[y0, x0] or visited[y0, x0]:
                continue
            stack = [(y0, x0)]
            visited[y0, x0] = True
            pixels: list[tuple[int, int]] = []
            touches_border = False
            while stack:
                y, x = stack.pop()
                pixels.append((y, x))
                if y == 0 or y == height - 1 or x == 0 or x == width - 1:
                    touches_border = True
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if dy == 0 and dx == 0:
                            continue
                        ny, nx = y + dy, x + dx
                        if (
                            0 <= ny < height
                            and 0 <= nx < width
                            and not solid[ny, nx]
                            and not visited[ny, nx]
                        ):
                            visited[ny, nx] = True
                            stack.append((ny, nx))
            if not touches_border and len(pixels) <= max_area:
                for y, x in pixels:
                    filled[y, x] = True
                filled_count += len(pixels)
    return filled, filled_count


def _chamfer_distance(target: np.ndarray) -> np.ndarray:
    """Approximate Euclidean distance to the nearest True pixel."""
    height, width = target.shape
    large = 1e9
    distance = np.where(target, 0.0, large)
    diagonal = math.sqrt(2.0)
    for y in range(1, height):
        for x in range(width):
            value = distance[y, x]
            if x > 0:
                value = min(value, distance[y - 1, x - 1] + diagonal, distance[y, x - 1] + 1.0)
            value = min(value, distance[y - 1, x] + 1.0)
            if x + 1 < width:
                value = min(value, distance[y - 1, x + 1] + diagonal)
            distance[y, x] = value
    for y in range(height - 2, -1, -1):
        for x in range(width - 1, -1, -1):
            value = distance[y, x]
            if x + 1 < width:
                value = min(value, distance[y + 1, x + 1] + diagonal, distance[y, x + 1] + 1.0)
            value = min(value, distance[y + 1, x] + 1.0)
            if x > 0:
                value = min(value, distance[y + 1, x - 1] + diagonal)
            distance[y, x] = value
    return distance


def _component_stats(binary: np.ndarray, cap: int) -> dict[str, Any]:
    visited = np.zeros(binary.shape, dtype=bool)
    areas: list[int] = []
    height, width = binary.shape
    for y0, x0 in np.argwhere(binary):
        if visited[y0, x0]:
            continue
        stack = [(int(y0), int(x0))]
        visited[y0, x0] = True
        area = 0
        while stack:
            y, x = stack.pop()
            area += 1
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dy == 0 and dx == 0:
                        continue
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < height and 0 <= nx < width and binary[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        stack.append((ny, nx))
        areas.append(area)
        if len(areas) > cap:
            break
    if not areas:
        return {
            "count": 0,
            "truncated": False,
            "largest_area": 0,
            "median_area": 0.0,
        }
    return {
        "count": len(areas),
        "truncated": bool(len(areas) > cap),
        "largest_area": int(max(areas)),
        "median_area": float(np.median(areas)),
        "smallest_area": int(min(areas)),
    }


def _ring_values(array: np.ndarray, center: tuple[int, int], radius: float) -> np.ndarray:
    angle_count = 720
    angles = np.linspace(0.0, 2.0 * math.pi, angle_count, endpoint=False)
    cx, cy = center
    xs = np.rint(cx + radius * np.cos(angles)).astype(np.int64)
    ys = np.rint(cy + radius * np.sin(angles)).astype(np.int64)
    valid = (xs >= 0) & (xs < array.shape[1]) & (ys >= 0) & (ys < array.shape[0])
    values = np.zeros(angle_count, dtype=np.float64)
    if np.any(valid):
        values[valid] = array[ys[valid], xs[valid]]
    return values


def _runs(mask: np.ndarray) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Return circular contiguous True and False runs."""
    if mask.size == 0:
        return [], []
    if np.all(mask):
        return [(0, mask.size - 1)], []
    if not np.any(mask):
        return [], [(0, mask.size - 1)]
    transitions = np.flatnonzero(mask != np.roll(mask, 1))
    true_runs: list[tuple[int, int]] = []
    false_runs: list[tuple[int, int]] = []
    for index, start in enumerate(transitions):
        end = transitions[(index + 1) % transitions.size] - 1
        if end < start:
            end += mask.size
        length = end - start + 1
        value = bool(mask[start % mask.size])
        if value:
            true_runs.append((start, start + length - 1))
        else:
            false_runs.append((start, start + length - 1))
    return true_runs, false_runs


def _tooth_anomalies(
    source_ink: np.ndarray,
    solid: np.ndarray,
    part_mask: np.ndarray,
    region: dict[str, Any],
    *,
    root_gap_min_px: float,
    solid_threshold: float,
) -> dict[str, Any]:
    center = region.get("center")
    root = region.get("root_radius_px")
    tip = region.get("tip_radius_px")
    expected = region.get("tooth_count")
    if not (
        isinstance(center, list)
        and len(center) == 2
        and isinstance(root, (int, float))
        and isinstance(tip, (int, float))
        and isinstance(expected, int)
    ):
        return {"skipped": True, "reason": "incomplete_tooth_truth"}
    cx, cy = int(center[0]), int(center[1])
    root_radius = float(root)
    tip_radius = float(tip)
    pitch_radius = 0.5 * (root_radius + tip_radius)
    root_sample_radius = max(1.0, root_radius - max(1.0, min(8.0, root_radius * 0.10)))
    root_values = _ring_values(solid, (cx, cy), root_sample_radius)
    pitch_values = _ring_values(solid, (cx, cy), pitch_radius)
    mask_values = _ring_values(part_mask.astype(np.float64), (cx, cy), pitch_radius)
    root_threshold = max(0.18, 0.50 * float(np.median(root_values)))
    pitch_threshold = 0.5 * (
        float(np.percentile(pitch_values, 10)) + float(np.percentile(pitch_values, 90))
    )
    root_true, root_false = _runs(root_values >= root_threshold)
    pitch_runs, _ = _runs(pitch_values >= pitch_threshold)
    mask_runs, _ = _runs(mask_values >= 0.5)
    arc_per_sample = 2.0 * math.pi * root_sample_radius / max(root_values.size, 1)
    root_gap_threshold = max(1, int(math.ceil(root_gap_min_px / max(arc_per_sample, 1e-6))))
    significant_root_gaps = [run for run in root_false if run[1] - run[0] + 1 >= root_gap_threshold]
    root_break_count = len(significant_root_gaps)
    detected_tip_runs = len(pitch_runs)
    reference_tip_runs = len(mask_runs)
    tip_adhesions = max(0, reference_tip_runs - detected_tip_runs)
    reference_root_values = _ring_values(source_ink, (cx, cy), root_sample_radius)
    reference_root_continuous = bool(np.mean(reference_root_values >= 0.42) > 0.95)
    return {
        "skipped": False,
        "root_sample_radius_px": root_sample_radius,
        "pitch_sample_radius_px": pitch_radius,
        "root_gap_threshold_samples": root_gap_threshold,
        "root_threshold": float(root_threshold),
        "root_gap_count": int(len(significant_root_gaps)),
        "root_gap_runs_samples": [[int(a), int(b)] for a, b in significant_root_gaps],
        "pitch_threshold": float(pitch_threshold),
        "reference_tip_runs": int(reference_tip_runs),
        "reference_root_expected_continuous": reference_root_continuous,
        "root_break_count": int(root_break_count),
        "expected_tip_segments": int(expected),
        "detected_tip_segments": int(detected_tip_runs),
        "tooth_tip_adhesion_count": int(tip_adhesions),
        "truth_root_breaks": region.get("truth", {}).get("expected_root_breaks"),
        "truth_tip_adhesions": region.get("truth", {}).get("expected_tip_adhesions"),
    }


def _bore_anomalies(
    binary: np.ndarray,
    smooth_full: np.ndarray,
    region: dict[str, Any],
    *,
    open_max_ink: float,
) -> dict[str, Any]:
    center = region.get("center")
    radius = region.get("bore_radius_px")
    if not (isinstance(center, list) and len(center) == 2 and isinstance(radius, (int, float))):
        return {"skipped": True, "reason": "incomplete_bore_truth"}
    cx, cy = int(center[0]), int(center[1])
    rr = max(1.0, float(radius) * 0.72)
    y0 = max(0, int(math.floor(cy - rr)))
    y1 = min(binary.shape[0], int(math.ceil(cy + rr)) + 1)
    x0 = max(0, int(math.floor(cx - rr)))
    x1 = min(binary.shape[1], int(math.ceil(cx + rr)) + 1)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    disk = (yy - cy) ** 2 + (xx - cx) ** 2 <= rr**2
    central_ink = float(smooth_full[y0:y1, x0:x1][disk].mean()) if np.any(disk) else 0.0
    closed = central_ink > open_max_ink
    return {
        "skipped": False,
        "bore_center": [cx, cy],
        "sample_radius_px": float(rr),
        "central_mean_ink": central_ink,
        "open_max_ink": float(open_max_ink),
        "hole_closed": bool(closed),
        "bore_closure_count": int(closed),
        "truth_hole_open": region.get("truth", {}).get("expected_hole_open"),
    }


def build_detail_report(bundle: InputBundle, final: DiffusionResult) -> dict[str, Any]:
    cfg = bundle.config["detail"]
    smoothing = _odd_smoothing_size(bundle)
    dot_px = float(bundle.print_grid["ink_dot_diameter_px"])
    edge_tone = float(cfg["edge_tone"])
    solid_threshold = float(cfg["solid_threshold"])
    max_hole_area = int(
        float(cfg["max_hole_fill_fraction_of_smoothing_area"]) * smoothing * smoothing
    )
    tolerance = max(
        float(cfg["edge_tolerance_min_px"]),
        float(cfg["edge_tolerance_dot_radii"]) * dot_px * 0.5,
        0.35 * smoothing,
    )
    p95_multiple = float(cfg["edge_p95_tolerance_multiple"])
    source_ink = 1.0 - bundle.gray.astype(np.float64) / 255.0
    smooth_full = _box_mean(final.binary.astype(np.float64), smoothing)

    regions: list[dict[str, Any]] = []
    all_ok = True
    for region in bundle.detail_regions:
        label = int(region["part_label"])
        x, y, width, height = region["bbox"]
        part = bundle.mask[y : y + height, x : x + width] == label
        isolation = region.get("isolation_radius_px")
        if isinstance(isolation, (int, float)):
            yy, xx = np.mgrid[y : y + height, x : x + width]
            center = region.get("center")
            if isinstance(center, list) and len(center) == 2:
                iso_mask = (xx - int(center[0])) ** 2 + (yy - int(center[1])) ** 2 <= float(isolation) ** 2
                part = part & iso_mask
        source_roi = source_ink[y : y + height, x : x + width]
        smooth_roi = smooth_full[y : y + height, x : x + width]
        binary_roi = final.binary[y : y + height, x : x + width] == 1
        predicted = (smooth_roi >= solid_threshold) & part
        predicted, filled_hole_pixels = _fill_small_holes(predicted, max_hole_area)
        reference = (source_roi >= edge_tone) & part
        predicted_edge = _edge_points(predicted)
        reference_edge = _edge_points(reference)
        ref_distance = _chamfer_distance(reference_edge)
        pred_distance = _chamfer_distance(predicted_edge)
        pred_to_ref = ref_distance[predicted_edge]
        ref_to_pred = pred_distance[reference_edge]
        if pred_to_ref.size == 0 or ref_to_pred.size == 0:
            edge_stats = {
                "available": False,
                "reason": "predicted_or_reference_edge_empty",
                "predicted_edge_pixels": int(predicted_edge.sum()),
                "reference_edge_pixels": int(reference_edge.sum()),
            }
            edge_pass = False
        else:
            combined = np.concatenate([pred_to_ref, ref_to_pred])
            edge_stats = {
                "available": True,
                "predicted_edge_pixels": int(predicted_edge.sum()),
                "reference_edge_pixels": int(reference_edge.sum()),
                "predicted_to_reference_mean_px": float(np.mean(pred_to_ref)),
                "predicted_to_reference_p95_px": float(np.percentile(pred_to_ref, 95)),
                "reference_to_predicted_mean_px": float(np.mean(ref_to_pred)),
                "reference_to_predicted_p95_px": float(np.percentile(ref_to_pred, 95)),
                "symmetric_mean_abs_offset_px": float(np.mean(combined)),
                "symmetric_max_abs_offset_px": float(np.max(combined)),
                "normalized_by_dot_diameter": float(np.mean(combined) / max(dot_px, 1e-9)),
                "normalized_by_dot_radius": float(np.mean(combined) / max(dot_px * 0.5, 1e-9)),
                "tolerance_px": tolerance,
                "tolerance_calibration": "max(min_px, edge_tolerance_dot_radii * dot_radius)",
            }
            edge_pass = bool(
                edge_stats["symmetric_mean_abs_offset_px"] <= tolerance
                and edge_stats["predicted_to_reference_p95_px"] <= tolerance * p95_multiple
            )

        raw_components = _component_stats(
            binary_roi & part,
            int(cfg["max_raw_dot_components_report"]),
        )
        anomalies: dict[str, Any] = {}
        if region.get("kind") == "tooth":
            anomalies = _tooth_anomalies(
                source_ink,
                smooth_full,
                bundle.mask == label,
                region,
                root_gap_min_px=float(cfg["root_gap_min_px"]),
                solid_threshold=solid_threshold,
            )
            anomaly_ok = (
                not anomalies.get("skipped", False)
                and anomalies.get("root_break_count", 1) == 0
                and anomalies.get("tooth_tip_adhesion_count", 1) == 0
            )
        elif region.get("kind") == "bore":
            anomalies = _bore_anomalies(
                final.binary,
                smooth_full,
                region,
                open_max_ink=float(cfg["bore_open_max_ink"]),
            )
            anomaly_ok = not anomalies.get("skipped", False) and not anomalies.get("hole_closed", True)
        else:
            anomaly_ok = True

        region_ok = bool(edge_pass and anomaly_ok)
        all_ok = all_ok and region_ok
        regions.append(
            {
                "id": region.get("id"),
                "part_label": label,
                "kind": region.get("kind"),
                "bbox_xywh": region["bbox"],
                "measurement_scale": {
                    "screen_pitch_px": 0.5 * sum(bundle.print_grid["screen_pitch_px"]),
                    "ink_dot_diameter_px": dot_px,
                    "ink_dot_radius_px": 0.5 * dot_px,
                    "smoothing_window_px": smoothing,
                    "solid_threshold": solid_threshold,
                    "max_filled_hole_px": max_hole_area,
                    "edge_tone": edge_tone,
                    "edge_tolerance_px": tolerance,
                    "edge_p95_tolerance_px": tolerance * p95_multiple,
                },
                "edge_offset": edge_stats,
                "raw_dot_connectivity": raw_components,
                "filled_speckle_hole_pixels": int(filled_hole_pixels),
                "anomalies": anomalies,
                "truth": region.get("truth", {}),
                "passed": region_ok,
            }
        )

    return {
        "schema": "clock-halftone-detail-fidelity/v1",
        "ok": bool(all_ok),
        "scale_policy": {
            "formula": "smoothing = odd(round(max(min_px, cell_pitch*2.0, dot_diameter*3.0)))",
            "edge_tolerance_formula": "max(min_px, edge_tolerance_dot_radii*dot_radius, 0.35*smoothing)",
            "edge_tolerance_px": tolerance,
            "normalized_metrics": ["normalized_by_dot_diameter", "normalized_by_dot_radius"],
            "ink_dot_diameter_px": dot_px,
        },
        "regions": regions,
    }
