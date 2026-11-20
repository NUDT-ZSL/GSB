"""Stage 1: deterministic axis and tick localization.

Axes are pure black (0,0,0); series colors always have a channel >= 100,
so a strict black mask separates them. Tick marks are short horizontal
segments to the left of the y-axis. For identical input and config the
output tick set is byte-identical; ties are broken by ascending pixel
coordinate along the axis direction.
"""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
from PIL import Image

from . import codes
from .errors import BusinessError

STAGE = "tick_localization"


def _black_mask(img: Image.Image, threshold: int) -> np.ndarray:
    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
    return (arr[:, :, 0] < threshold) & (arr[:, :, 1] < threshold) & (arr[:, :, 2] < threshold)


def _group_runs(indices: List[int]) -> List[float]:
    """Group consecutive integer indices; return group centers (means)."""
    groups: List[List[int]] = []
    for idx in indices:
        if groups and idx == groups[-1][-1] + 1:
            groups[-1].append(idx)
        else:
            groups.append([idx])
    return [float(sum(g)) / len(g) for g in groups]


def locate_ticks(img: Image.Image, truth: Dict[str, Any], cfg: Dict[str, Any]
                 ) -> Dict[str, Any]:
    chart_id = truth["chart_id"]
    height, width = truth["canvas_shape"]
    mask = _black_mask(img, cfg["axis_black_threshold"])

    col_counts = mask.sum(axis=0)
    row_counts = mask.sum(axis=1)
    axis_x = int(np.argmax(col_counts))          # first max wins: deterministic
    baseline_y = int(np.argmax(row_counts))

    tick_len = cfg["tick_length"]
    lo = max(0, axis_x - tick_len)
    band = mask[:, lo:axis_x]
    rows_with_tick = [
        y for y in range(height)
        if int(band[y].sum()) >= cfg["tick_min_dark_run"]
    ]
    tick_pixels = sorted(_group_runs(rows_with_tick))

    for tp in tick_pixels:
        if not (0.0 <= tp <= height - 1):
            raise BusinessError(
                codes.E_TICK_LOC_INVALID,
                f"tick pixel {tp} outside canvas height {height}",
                chart_id=chart_id, stage=STAGE, detail={"tick_pixel": tp},
            )

    declared = len(truth["axis"]["tick_pixels"])
    count_ok = len(tick_pixels) == declared

    return {
        "chart_id": chart_id,
        "axis_x": axis_x,
        "baseline_y": baseline_y,
        "tick_pixels": tick_pixels,
        "tick_count": len(tick_pixels),
        "declared_tick_count": declared,
        "count_matches_truth": count_ok,
    }


def validate_tick_count(tick_result: Dict[str, Any]) -> None:
    if not tick_result["count_matches_truth"]:
        raise BusinessError(
            codes.E_TICK_COUNT_MISMATCH,
            f"detected {tick_result['tick_count']} ticks, truth declares "
            f"{tick_result['declared_tick_count']}",
            chart_id=tick_result["chart_id"],
            stage=STAGE,
        )


def validate_tick_pixels(tick_result: Dict[str, Any], truth: Dict[str, Any],
                         cfg: Dict[str, Any]) -> None:
    """Bounds check, re-run after any input-side fault injection."""
    height = truth["canvas_shape"][0]
    for tp in tick_result["tick_pixels"]:
        if not (0.0 <= tp <= height - 1):
            raise BusinessError(
                codes.E_TICK_LOC_INVALID,
                f"tick pixel {tp} outside canvas height {height}",
                chart_id=tick_result["chart_id"], stage=STAGE,
                detail={"tick_pixel": tp},
            )
