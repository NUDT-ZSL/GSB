from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from .config import AnalysisConfig


def _active_runs(flags: np.ndarray, merge_gap: int) -> list[tuple[int, int]]:
    positions = np.flatnonzero(flags)
    if positions.size == 0:
        return []
    runs: list[tuple[int, int]] = []
    start = int(positions[0])
    end = start + 1
    for raw_position in positions[1:]:
        position = int(raw_position)
        if position - end <= merge_gap:
            end = position + 1
        else:
            runs.append((start, end))
            start = position
            end = position + 1
    runs.append((start, end))
    return runs


def downsample_min(gray: np.ndarray, step: int) -> np.ndarray:
    if step <= 1:
        return gray
    height, width = gray.shape
    usable_h = (height // step) * step
    usable_w = (width // step) * step
    if usable_h <= 0 or usable_w <= 0:
        return gray
    shape = (usable_h // step, step, usable_w // step, step)
    return gray[:usable_h, :usable_w].reshape(shape).min(axis=(1, 3))


def horizontal_projection_periodicity(mask: np.ndarray) -> float:
    if mask.shape[0] <= 4 or mask.shape[1] <= 4:
        return 0.0
    interior = mask[1:-1, 1:-1]
    row_counts = interior.sum(axis=1).astype(np.float64)
    if row_counts.size < 5:
        return 0.0
    centered = row_counts - row_counts.mean()
    energy = float(np.dot(centered, centered))
    if energy <= 1e-9:
        return 0.0
    max_lag = max(2, min(32, interior.shape[0] // 3))
    best = 0.0
    for lag in range(2, max_lag + 1):
        if lag >= centered.size:
            break
        numerator = float(np.dot(centered[:-lag], centered[lag:]))
        denominator = energy
        if denominator > 0:
            best = max(best, numerator / denominator)
    return round(max(0.0, min(1.0, best)), 6)


def detect_blocks(
    gray: np.ndarray,
    *,
    page_name: str,
    config: AnalysisConfig,
    resolution_step: int = 1,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    step = max(1, int(resolution_step))
    original_height, original_width = gray.shape
    work_gray = downsample_min(gray, step)
    mask = work_gray < config.binary_threshold
    window = max(1, config.projection_analysis_window // step)
    minimum_width = max(1, config.min_block_width // step)
    minimum_height = max(1, config.min_block_height // step)

    raw_candidates: list[dict[str, Any]] = []
    row_runs = _active_runs(mask.any(axis=1), window)
    for row_start, row_end in row_runs:
        column_runs = _active_runs(mask[row_start:row_end, :].any(axis=0), window)
        for column_start, column_end in column_runs:
            region = mask[row_start:row_end, column_start:column_end]
            inner_row_runs = _active_runs(region.any(axis=1), window)
            for inner_row_start, inner_row_end in inner_row_runs:
                narrow = region[inner_row_start:inner_row_end, :]
                inner_column_runs = _active_runs(narrow.any(axis=0), window)
                for inner_column_start, inner_column_end in inner_column_runs:
                    cell = region[
                        inner_row_start:inner_row_end,
                        inner_column_start:inner_column_end,
                    ]
                    row_indices = np.flatnonzero(cell.any(axis=1))
                    column_indices = np.flatnonzero(cell.any(axis=0))
                    if row_indices.size == 0 or column_indices.size == 0:
                        continue
                    y0 = row_start + inner_row_start + int(row_indices[0])
                    y1 = row_start + inner_row_start + int(row_indices[-1]) + 1
                    x0 = column_start + inner_column_start + int(column_indices[0])
                    x1 = column_start + inner_column_start + int(column_indices[-1]) + 1
                    trimmed = mask[y0:y1, x0:x1]
                    raw_candidates.append(
                        {
                            "x": x0,
                            "y": y0,
                            "w": x1 - x0,
                            "h": y1 - y0,
                            "density": float(trimmed.mean()),
                            "periodicity": horizontal_projection_periodicity(trimmed),
                        }
                    )

    filtered: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for candidate in raw_candidates:
        width = int(candidate["w"])
        height = int(candidate["h"])
        if width < minimum_width or height < minimum_height:
            continue
        x = int(candidate["x"]) * step
        y = int(candidate["y"]) * step
        scaled_width = min(width * step, original_width - x)
        scaled_height = min(height * step, original_height - y)
        if scaled_width < config.min_block_width or scaled_height < config.min_block_height:
            continue
        bbox = (x, y, scaled_width, scaled_height)
        if bbox in seen:
            continue
        seen.add(bbox)
        filtered.append(
            {
                "bbox": [x, y, scaled_width, scaled_height],
                "ink_density": round(float(candidate["density"]), 6),
                "horizontal_projection_periodicity": float(candidate["periodicity"]),
            }
        )

    filtered.sort(key=lambda item: (item["bbox"][1], item["bbox"][0], item["bbox"][2], item["bbox"][3]))
    blocks: list[dict[str, Any]] = []
    for index, candidate in enumerate(filtered, start=1):
        blocks.append(
            {
                "block_id": f"b{index:03d}",
                "bbox": candidate["bbox"],
                "ink_density": candidate["ink_density"],
                "horizontal_projection_periodicity": candidate[
                    "horizontal_projection_periodicity"
                ],
                "source_page": page_name,
            }
        )

    diagnostics = {
        "resolution_step": step,
        "working_width": int(work_gray.shape[1]),
        "working_height": int(work_gray.shape[0]),
        "projection_window": window,
        "binary_threshold": config.binary_threshold,
        "ink_pixel_ratio": round(float(mask.mean()), 6),
        "raw_candidate_count": len(raw_candidates),
        "filtered_candidate_count": len(raw_candidates) - len(filtered),
        "block_count": len(blocks),
    }
    return blocks, diagnostics


def iter_bboxes(blocks: Iterable[dict[str, Any]]) -> list[tuple[int, int, int, int]]:
    return [tuple(int(value) for value in block["bbox"]) for block in blocks]  # type: ignore[return-value]
