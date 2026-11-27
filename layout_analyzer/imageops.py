from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


@dataclass
class Component:
    cid: int
    bbox: tuple[int, int, int, int]
    area: int
    mean_contrast: float


@dataclass
class Foreground:
    mask: np.ndarray
    labels: np.ndarray
    components: list[Component]
    diagnostics: dict[str, Any]


def load_rgb(path: str | Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.uint8)


def grayscale(rgb: np.ndarray) -> np.ndarray:
    return (
        0.299 * rgb[:, :, 0].astype(np.float32)
        + 0.587 * rgb[:, :, 1].astype(np.float32)
        + 0.114 * rgb[:, :, 2].astype(np.float32)
    )


def extract_foreground(rgb: np.ndarray, scale: float) -> Foreground:
    gray = grayscale(rgb)
    height, width = gray.shape
    border_width = max(2, int(round(5 * scale)))
    border = np.concatenate(
        [
            gray[:border_width, :].ravel(),
            gray[-border_width:, :].ravel(),
            gray[:, :border_width].ravel(),
            gray[:, -border_width:].ravel(),
        ]
    )
    background = float(np.median(border))
    robust_sigma = float(np.median(np.abs(border - background)) * 1.4826)
    strong_drop = max(30.0, 3.4 * robust_sigma)
    weak_drop = max(14.0, 2.2 * robust_sigma)
    strong = gray <= background - strong_drop
    weak = gray <= background - weak_drop

    # Hysteresis expansion preserves antialiased hairlines and JPEG/WebP edge
    # halos while preventing low-contrast paper texture from becoming a block.
    retained = strong.copy()
    for _ in range(3):
        retained = (_binary_dilate(retained, 4) & weak) | strong

    labels, raw_components, raw_count = _label_components(retained, gray, background)
    min_area = max(3, int(round(4.0 * scale * scale)))
    min_mean_contrast = max(18.0, weak_drop + 4.0)
    retained_components = [
        component
        for component in raw_components
        if component.area >= min_area and component.mean_contrast >= min_mean_contrast
    ]
    lookup = np.full(len(raw_components) if raw_components else 1, -1, dtype=np.int32)
    for new_id, component in enumerate(retained_components):
        lookup[component.cid] = new_id
    safe_labels = np.maximum(labels, 0)
    remapped = np.where(labels >= 0, lookup[safe_labels], -1)

    components = [
        Component(
            cid=new_id,
            bbox=component.bbox,
            area=component.area,
            mean_contrast=component.mean_contrast,
        )
        for new_id, component in enumerate(retained_components)
    ]
    mask = remapped >= 0
    diagnostics = {
        "background_gray": round(background, 3),
        "robust_sigma": round(robust_sigma, 3),
        "strong_drop": round(strong_drop, 3),
        "weak_drop": round(weak_drop, 3),
        "threshold_strong": round(background - strong_drop, 3),
        "threshold_weak": round(background - weak_drop, 3),
        "hysteresis_iterations": 3,
        "raw_component_count": raw_count,
        "retained_component_count": len(components),
        "min_component_area_px": min_area,
        "min_component_mean_contrast": round(min_mean_contrast, 3),
    }
    return Foreground(mask, remapped, components, diagnostics)


def _binary_dilate(mask: np.ndarray, connectivity: int = 4) -> np.ndarray:
    result = mask.copy()
    result[1:, :] |= mask[:-1, :]
    result[:-1, :] |= mask[1:, :]
    result[:, 1:] |= mask[:, :-1]
    result[:, :-1] |= mask[:, 1:]
    if connectivity == 8:
        result[1:, 1:] |= mask[:-1, :-1]
        result[:-1, :-1] |= mask[1:, 1:]
        result[1:, :-1] |= mask[:-1, 1:]
        result[:-1, 1:] |= mask[1:, :-1]
    return result


def _label_components(
    mask: np.ndarray,
    gray: np.ndarray,
    background: float,
) -> tuple[np.ndarray, list[Component], int]:
    height, width = mask.shape
    labels = np.full((height, width), -1, dtype=np.int32)
    parent: list[int] = []
    run_bbox: list[tuple[int, int, int, int]] = []
    run_ids: list[int] = []
    previous_runs: list[tuple[int, int, int]] = []
    next_id = 0

    def find(value: int) -> int:
        root = value
        while parent[root] != root:
            root = parent[root]
        while parent[value] != value:
            nxt = parent[value]
            parent[value] = root
            value = nxt
        return root

    def union(a: int, b: int) -> int:
        ra, rb = find(a), find(b)
        if ra == rb:
            return ra
        if ra < rb:
            parent[rb] = ra
            return ra
        parent[ra] = rb
        return rb

    for y in range(height):
        row = mask[y]
        padded = np.concatenate(([False], row, [False]))
        changes = np.flatnonzero(padded[1:] != padded[:-1])
        starts = changes[0::2]
        ends = changes[1::2]
        current_runs: list[tuple[int, int, int]] = []
        for start, end in zip(starts.tolist(), ends.tolist()):
            run_id = next_id
            next_id += 1
            parent.append(run_id)
            run_bbox.append((start, y, end, y + 1))
            run_ids.append(run_id)
            labels[y, start:end] = run_id
            current_runs.append((start, end, run_id))
        for current_index, previous_index in _overlapping_previous_runs(previous_runs, current_runs):
            union(current_runs[current_index][2], previous_runs[previous_index][2])
        previous_runs = current_runs

    if next_id == 0:
        return labels, [], 0

    roots_for_runs = np.array([find(index) for index in range(next_id)], dtype=np.int32)
    root_labels = np.full(labels.shape, -1, dtype=np.int32)
    valid_labels = labels >= 0
    root_labels[valid_labels] = roots_for_runs[labels[valid_labels]]
    roots = np.unique(roots_for_runs)
    root_to_index = {int(root): index for index, root in enumerate(roots.tolist())}

    foreground = root_labels >= 0
    flat_roots = root_labels[foreground].ravel()
    flat_root_indices = np.searchsorted(roots, flat_roots)
    flat_contrast = np.maximum(background - gray, 0.0)[foreground].ravel()
    area = np.bincount(flat_root_indices, minlength=len(roots))
    sum_contrast = np.bincount(
        flat_root_indices, weights=flat_contrast, minlength=len(roots)
    )

    bbox_min_x = np.full(len(roots), width, dtype=np.int32)
    bbox_min_y = np.full(len(roots), height, dtype=np.int32)
    bbox_max_x = np.zeros(len(roots), dtype=np.int32)
    bbox_max_y = np.zeros(len(roots), dtype=np.int32)
    for run_id, (start, y, end, _) in zip(run_ids, run_bbox):
        index = root_to_index[int(find(run_id))]
        if start < bbox_min_x[index]:
            bbox_min_x[index] = start
        if end > bbox_max_x[index]:
            bbox_max_x[index] = end
        if y < bbox_min_y[index]:
            bbox_min_y[index] = y
        if y + 1 > bbox_max_y[index]:
            bbox_max_y[index] = y + 1

    components: list[Component] = []
    for index, root in enumerate(roots.tolist()):
        if area[index] <= 0:
            continue
        components.append(
            Component(
                cid=index,
                bbox=(
                    int(bbox_min_x[index]),
                    int(bbox_min_y[index]),
                    int(bbox_max_x[index]),
                    int(bbox_max_y[index]),
                ),
                area=int(area[index]),
                mean_contrast=float(sum_contrast[index] / area[index]),
            )
        )

    dense = np.full(int(roots.max()) + 1, -1, dtype=np.int32)
    for dense_id, root in enumerate(roots.tolist()):
        dense[root] = dense_id
    compact = np.where(root_labels >= 0, np.take(dense, root_labels, mode="clip"), -1)
    compact[root_labels < 0] = -1
    return compact, components, len(roots)


def _overlapping_previous_runs(
    previous: list[tuple[int, int, int]],
    current: list[tuple[int, int, int]],
) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    p = 0
    for ci, (cs, ce, _) in enumerate(current):
        while p < len(previous) and previous[p][1] < cs - 1:
            p += 1
        q = p
        while q < len(previous) and previous[q][0] <= ce + 1:
            pairs.append((ci, q))
            q += 1
    return pairs


def projection_profiles(mask: np.ndarray, scale: float) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    x_profile = mask.sum(axis=0).astype(np.int32)
    y_profile = mask.sum(axis=1).astype(np.int32)
    kernel = max(1, int(round(3 * scale)))
    if kernel % 2 == 0:
        kernel += 1
    x_smooth = _moving_average(x_profile, kernel)
    y_smooth = _moving_average(y_profile, kernel)
    x_threshold = max(2.0, float(x_smooth.max()) * 0.045) if x_smooth.size else 2.0
    min_valley = max(2, int(round(30 * scale)))
    x_valleys = _find_valleys(x_smooth, x_threshold, min_valley)
    y_threshold = max(2.0, float(y_smooth.max()) * 0.10) if y_smooth.size else 2.0
    y_valleys = _find_valleys(y_smooth, y_threshold, max(2, int(round(16 * scale))))
    diagnostics = {
        "x_profile_peak": int(x_profile.max()) if x_profile.size else 0,
        "y_profile_peak": int(y_profile.max()) if y_profile.size else 0,
        "x_valley_threshold": round(x_threshold, 3),
        "y_valley_threshold": round(y_threshold, 3),
        "min_x_valley_width_px": min_valley,
        "x_valleys": [[int(a), int(b)] for a, b in x_valleys],
        "y_valleys": [[int(a), int(b)] for a, b in y_valleys],
    }
    return x_smooth, y_smooth, diagnostics


def _moving_average(values: np.ndarray, kernel: int) -> np.ndarray:
    if values.size == 0:
        return values.astype(np.float32)
    if kernel <= 1:
        return values.astype(np.float32)
    pad = kernel // 2
    padded = np.pad(values.astype(np.float32), (pad, pad), mode="edge")
    cumulative = np.concatenate(([0.0], np.cumsum(padded, dtype=np.float64)))
    return ((cumulative[kernel:] - cumulative[:-kernel]) / kernel).astype(np.float32)


def _find_valleys(
    profile: np.ndarray,
    threshold: float,
    min_width: int,
) -> list[tuple[int, int]]:
    low = profile <= threshold
    padded = np.concatenate(([False], low, [False]))
    changes = np.flatnonzero(padded[1:] != padded[:-1])
    valleys = []
    for start, end in zip(changes[0::2], changes[1::2]):
        if end - start >= min_width:
            valleys.append((int(start), int(end)))
    return valleys
