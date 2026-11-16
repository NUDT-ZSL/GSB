"""Diagnostic report construction and dependency-graph rendering."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageDraw

from .canonical import atomic_write_bytes
from .pipeline import StageSpec


def pixel_diff_summary(previous: np.ndarray | None, current: np.ndarray) -> dict[str, Any]:
    current_arr = np.asarray(current, dtype=np.uint8)
    if previous is None:
        return {
            "available": False,
            "identical": None,
            "shape": list(current_arr.shape),
            "changed_pixels": None,
            "changed_channels": None,
            "mean_abs_diff": None,
            "max_abs_diff": None,
            "identical_pixel_ratio": None,
        }
    previous_arr = np.asarray(previous, dtype=np.uint8)
    if previous_arr.shape != current_arr.shape:
        return {
            "available": True,
            "identical": False,
            "shape": list(current_arr.shape),
            "previous_shape": list(previous_arr.shape),
            "shape_changed": True,
            "changed_pixels": int(current_arr.shape[0] * current_arr.shape[1]),
            "changed_channels": int(current_arr.size),
            "mean_abs_diff": None,
            "max_abs_diff": None,
            "identical_pixel_ratio": 0.0,
        }
    difference = np.abs(previous_arr.astype(np.int16) - current_arr.astype(np.int16))
    changed_channels = int(np.count_nonzero(difference))
    changed_pixels = int(np.count_nonzero(np.any(difference > 0, axis=2)))
    total_pixels = int(current_arr.shape[0] * current_arr.shape[1])
    return {
        "available": True,
        "identical": changed_channels == 0,
        "shape": list(current_arr.shape),
        "shape_changed": False,
        "changed_pixels": changed_pixels,
        "changed_channels": changed_channels,
        "mean_abs_diff": round(float(difference.sum()) / max(int(current_arr.size), 1), 6),
        "max_abs_diff": int(difference.max()) if difference.size else 0,
        "identical_pixel_ratio": round(
            (total_pixels - changed_pixels) / max(total_pixels, 1), 6
        ),
    }


def _layout(specs: list[StageSpec]) -> tuple[dict[str, int], dict[str, int], int]:
    levels: dict[str, int] = {}
    for spec in specs:
        dependency_levels = [levels[dependency] for dependency in spec.dependencies if dependency in levels]
        levels[spec.stage_id] = (max(dependency_levels) + 1) if dependency_levels else 0
    rows: dict[str, int] = {}
    per_level: dict[int, int] = {}
    for spec in specs:
        level = levels[spec.stage_id]
        row = per_level.get(level, 0)
        rows[spec.stage_id] = row
        per_level[level] = row + 1
    return levels, rows, max(per_level.values(), default=1)


def _status_style(status: str) -> tuple[str, str, str]:
    if status == "reused":
        return "#d9f5df", "#1b7f3b", "#155f2d"
    if status == "recomputed":
        return "#ffe0e0", "#c62828", "#8b1e1e"
    if status == "failed":
        return "#ffc4c4", "#8b0000", "#600000"
    if status == "hit":
        return "#d9f5df", "#1b7f3b", "#155f2d"
    return "#eeeeee", "#666666", "#333333"


def render_dependency_graph(
    specs: list[StageSpec],
    statuses: dict[str, str],
    timings_ms: dict[str, float],
    path: Path | str,
    *,
    snapshot_version: int | None,
    title_suffix: str = "",
) -> None:
    levels, rows, row_count = _layout(specs)
    max_level = max(levels.values(), default=0)
    node_width, node_height = 214, 58
    x_gap, y_gap = 84, 28
    margin_x, margin_y = 46, 84
    width = max(900, margin_x * 2 + (max_level + 1) * node_width + max_level * x_gap)
    height = max(430, margin_y + row_count * (node_height + y_gap) + 120)
    image = Image.new("RGB", (width, height), "#fbfbfd")
    draw = ImageDraw.Draw(image)

    version_text = f"v{snapshot_version}" if snapshot_version is not None else "no snapshot"
    title = f"Stage dependency graph - {version_text}"
    if title_suffix:
        title += f" - {title_suffix}"
    draw.text((margin_x, 26), title, fill="#202124")
    draw.text(
        (margin_x, 50),
        "green = reused/hit    red = invalidated/recomputed    dark red = failed",
        fill="#555b66",
    )

    positions: dict[str, tuple[int, int, int, int]] = {}
    for spec in specs:
        level = levels[spec.stage_id]
        row = rows[spec.stage_id]
        x0 = margin_x + level * (node_width + x_gap)
        y0 = margin_y + row * (node_height + y_gap)
        positions[spec.stage_id] = (x0, y0, x0 + node_width, y0 + node_height)

    for spec in specs:
        target = positions[spec.stage_id]
        for dependency in spec.dependencies:
            source = positions.get(dependency)
            if source is None:
                continue
            start = (source[2], (source[1] + source[3]) // 2)
            end = (target[0], (target[1] + target[3]) // 2)
            draw.line([start, end], fill="#8b93a1", width=2)
            direction = 1 if end[0] >= start[0] else -1
            arrow = [
                end,
                (end[0] - 8 * direction, end[1] - 5),
                (end[0] - 8 * direction, end[1] + 5),
            ]
            draw.polygon(arrow, fill="#8b93a1")

    for spec in specs:
        x0, y0, x1, y1 = positions[spec.stage_id]
        status = statuses.get(spec.stage_id, "invalidated")
        fill, border, text_color = _status_style(status)
        draw.rounded_rectangle((x0, y0, x1, y1), radius=9, fill=fill, outline=border, width=2)
        elapsed = float(timings_ms.get(spec.stage_id, 0.0))
        label = f"{spec.stage_id}\n{status}  {elapsed:.3f} ms"
        draw.multiline_text((x0 + 10, y0 + 9), label, fill=text_color, spacing=4)

    legend_y = height - 38
    draw.text(
        (margin_x, legend_y),
        "Edges point from dependency to dependent stage. Timings are recomputation durations.",
        fill="#555b66",
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    atomic_write_bytes(Path(path), buffer.getvalue())


def build_report(
    *,
    status: str,
    business_code: str | None,
    edits: list[dict[str, Any]],
    errors: list[dict[str, Any]] | None = None,
    snapshot_version: int | None = None,
    config_hash: str | None = None,
    pipeline: Iterable[str] | None = None,
    dependencies: dict[str, list[str]] | None = None,
    snapshot_path: str | None = None,
    final_image: str | None = None,
    global_content_fingerprint: str | None = None,
) -> dict[str, Any]:
    return {
        "report_version": 1,
        "status": status,
        "business_code": business_code,
        "snapshot_version": snapshot_version,
        "config_hash": config_hash,
        "global_content_fingerprint": global_content_fingerprint,
        "pipeline": list(pipeline or []),
        "dependencies": dependencies or {},
        "edits": edits,
        "errors": errors or [],
        "snapshot_path": snapshot_path,
        "final_image": final_image,
    }


def write_report(path: Path | str, report: dict[str, Any]) -> None:
    from .canonical import atomic_write_json

    atomic_write_json(path, report)

