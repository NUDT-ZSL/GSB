"""Cross-version overlay rendering, pixel diff and boundary diagnostics."""

from __future__ import annotations

import numpy as np

from .convert import DEFAULT_COLOR_TABLE
from .errors import E_BOUNDARY_SENSITIVE
from .model import RegionDesc


def _category_color(ir: RegionDesc, color_table: dict) -> tuple:
    if ir.color_name and ir.color_name in color_table:
        return tuple(int(c) for c in color_table[ir.color_name])
    return tuple(DEFAULT_COLOR_TABLE.get(ir.category, (128, 128, 128)))


def render_overlay(irs: list[RegionDesc], width: int, height: int,
                   color_table: dict | None = None) -> np.ndarray:
    """Render descriptions onto a blank canvas, filling bboxes by category color."""
    table = color_table or DEFAULT_COLOR_TABLE
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    for ir in sorted(irs, key=lambda r: r.sort_key):
        x0, y0, x1, y1 = ir.bbox
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(width, x1), min(height, y1)
        if x1 > x0 and y1 > y0:
            canvas[y0:y1, x0:x1] = _category_color(ir, table)
    return canvas


def diff_overlays(a: np.ndarray, b: np.ndarray) -> dict:
    """Per-pixel max channel difference and count of differing pixels."""
    delta = np.abs(a.astype(np.int16) - b.astype(np.int16))
    per_pixel = delta.max(axis=2)
    return {
        "max_channel_diff": int(per_pixel.max()) if per_pixel.size else 0,
        "diff_pixels": int((per_pixel > 0).sum()),
    }


def boundary_diagnostics(irs_v2: list[RegionDesc], config) -> list[dict]:
    """Flag regions sitting exactly on a decision boundary.

    A region is boundary-sensitive when its double-precision confidence lies
    within boundary_epsilon of the decision threshold, or any float coordinate
    has a fractional part of exactly 0.5 (maximal rounding residual).
    """
    threshold = config["confidence_threshold"]
    epsilon = config["boundary_epsilon"]
    findings = []
    for ir in sorted(irs_v2, key=lambda r: r.sort_key):
        reasons, half_pixel_axes = [], []
        orig = ir.orig_bbox or tuple(float(v) for v in ir.bbox)
        for axis, value in zip(("x0", "y0", "x1", "y1"), orig):
            frac = value - int(value)
            if abs(frac - 0.5) < 1e-12:
                half_pixel_axes.append({"axis": axis, "double_ref": repr(value)})
        if half_pixel_axes:
            reasons.append("half_pixel_rounding")
        conf_ref = ir.orig_confidence
        near_threshold = (conf_ref is not None
                          and abs(conf_ref - threshold) <= epsilon)
        if near_threshold:
            reasons.append("confidence_at_threshold")
        if not reasons:
            continue
        findings.append({
            "code": E_BOUNDARY_SENSITIVE,
            "inst_id": ir.inst_id,
            "reasons": reasons,
            "double_precision_ref": {
                "bbox": [repr(v) for v in orig],
                "confidence": repr(conf_ref),
            },
            "half_pixel_axes": half_pixel_axes,
            "version_decisions": {
                "v1": {"bbox": list(ir.bbox), "conf_permille": ir.conf_permille},
                "v2": {"bbox": list(orig), "confidence": conf_ref},
            },
            "threshold": threshold,
            "epsilon": epsilon,
        })
    return findings
