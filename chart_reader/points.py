"""Stage 3: data-point pixel localization and candidate de-duplication.

Line charts: vertices are drawn as filled square markers; a pixel is a
candidate center iff the (marker_size x marker_size) block around it is
fully painted with the series color (1px polylines never satisfy this).
Bar charts: each bar is one connected component of series color; the data
point is the midpoint of its top edge.

Candidates closer than `candidate_merge_distance` are merged by a fixed
rule: process candidates sorted by (x, y), merge into the running
centroid of the current cluster. Points are emitted per series sorted by
ascending x pixel; series never share candidates (disjoint colors).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

import numpy as np
from PIL import Image

from . import codes
from .errors import BusinessError

STAGE = "point_localization"


def _color_mask(img: Image.Image, color: Tuple[int, int, int]) -> np.ndarray:
    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
    return ((arr[:, :, 0] == color[0])
            & (arr[:, :, 1] == color[1])
            & (arr[:, :, 2] == color[2]))


def _label_components(mask: np.ndarray) -> List[List[Tuple[int, int]]]:
    """4-connected components of a boolean mask. Returns pixel lists."""
    height, width = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    components: List[List[Tuple[int, int]]] = []
    ys, xs = np.nonzero(mask)
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if visited[sy, sx]:
            continue
        stack = [(sy, sx)]
        visited[sy, sx] = True
        comp: List[Tuple[int, int]] = []
        while stack:
            y, x = stack.pop()
            comp.append((y, x))
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < height and 0 <= nx < width \
                        and mask[ny, nx] and not visited[ny, nx]:
                    visited[ny, nx] = True
                    stack.append((ny, nx))
        components.append(comp)
    return components


def _marker_centers(mask: np.ndarray, marker_size: int) -> List[Tuple[float, float]]:
    """Centroids of positions whose marker_size-block is fully painted."""
    half = marker_size // 2
    height, width = mask.shape
    if height < marker_size or width < marker_size:
        return []
    integral = np.zeros((height + 1, width + 1), dtype=np.int64)
    integral[1:, 1:] = mask.astype(np.int64).cumsum(axis=0).cumsum(axis=1)
    block = (integral[marker_size:, marker_size:]
             - integral[:-marker_size, marker_size:]
             - integral[marker_size:, :-marker_size]
             + integral[:-marker_size, :-marker_size])
    bys, bxs = np.nonzero(block == marker_size * marker_size)
    centers = list(zip((bys + half).tolist(), (bxs + half).tolist()))
    if not centers:
        return []
    center_mask = np.zeros_like(mask, dtype=bool)
    for (y, x) in centers:
        center_mask[y, x] = True
    out = []
    for comp in _label_components(center_mask):
        cy = sum(p[0] for p in comp) / len(comp)
        cx = sum(p[1] for p in comp) / len(comp)
        out.append((cx, cy))
    return out


def _bar_tops(mask: np.ndarray) -> List[Tuple[float, float, float]]:
    """(cx, top_y, fill_ratio) for each bar component."""
    out = []
    for comp in _label_components(mask):
        ys = [p[0] for p in comp]
        xs = [p[1] for p in comp]
        y0, y1 = min(ys), max(ys)
        x0, x1 = min(xs), max(xs)
        area = (y1 - y0 + 1) * (x1 - x0 + 1)
        fill = len(comp) / area if area else 0.0
        out.append(((x0 + x1) / 2.0, float(y0), fill))
    return out


def _merge_candidates(cands: List[Tuple[float, float, float]],
                      max_dist: float) -> List[Dict[str, float]]:
    """Fixed-rule merge: sort by (x, y); absorb near candidates into the
    running centroid of the open cluster."""
    ordered = sorted(cands, key=lambda c: (c[0], c[1]))
    merged: List[Dict[str, float]] = []
    for (cx, cy, conf) in ordered:
        if merged:
            last = merged[-1]
            if math.hypot(cx - last["pixel_x"], cy - last["pixel_y"]) < max_dist:
                n = last["merged_count"]
                last["pixel_x"] = (last["pixel_x"] * n + cx) / (n + 1)
                last["pixel_y"] = (last["pixel_y"] * n + cy) / (n + 1)
                last["confidence"] = (last["confidence"] * n + conf) / (n + 1)
                last["merged_count"] = n + 1
                continue
        merged.append({
            "pixel_x": cx,
            "pixel_y": cy,
            "confidence": conf,
            "merged_count": 1,
        })
    return merged


def locate_points(img: Image.Image, truth: Dict[str, Any], cfg: Dict[str, Any]
                  ) -> Dict[str, Any]:
    chart_id = truth["chart_id"]
    height, width = truth["canvas_shape"]
    marker_size = cfg["marker_size"]
    half = marker_size // 2

    series_results = []
    for spec in truth["series"]:
        series_id = spec["series_id"]
        color = tuple(spec["color"])
        mask = _color_mask(img, color)

        if truth["chart_kind"] == "line":
            raw = [(cx, cy, 1.0) for (cx, cy) in _marker_centers(mask, marker_size)]
        else:
            raw = [(cx, cy, fill) for (cx, cy, fill) in _bar_tops(mask)]

        candidates = _merge_candidates(raw, cfg["candidate_merge_distance"])

        for cand in candidates:
            px, py = cand["pixel_x"], cand["pixel_y"]
            if not (0 <= px <= width - 1) or not (0 <= py <= height - 1):
                raise BusinessError(
                    codes.E_POINT_OUT_OF_CANVAS,
                    f"candidate ({px}, {py}) outside canvas {width}x{height}",
                    chart_id=chart_id, stage=STAGE, series_id=series_id,
                    detail={"pixel_x": px, "pixel_y": py},
                )
            # Confidence: local fill ratio of the marker block (line) or
            # bounding-box fill (bar), both already deterministic.
            if truth["chart_kind"] == "line":
                iy, ix = int(round(py)), int(round(px))
                y0, y1 = max(0, iy - half), min(height, iy + half + 1)
                x0, x1 = max(0, ix - half), min(width, ix + half + 1)
                block = mask[y0:y1, x0:x1]
                cand["confidence"] = float(block.sum()) / float(block.size)

        if not candidates:
            raise BusinessError(
                codes.E_SERIES_INVALID,
                f"series {series_id} produced no candidates",
                chart_id=chart_id, stage=STAGE, series_id=series_id,
            )
        if len(candidates) > cfg["max_candidates_per_series"]:
            raise BusinessError(
                codes.E_SERIES_INVALID,
                f"series {series_id} produced {len(candidates)} candidates "
                f"(max {cfg['max_candidates_per_series']})",
                chart_id=chart_id, stage=STAGE, series_id=series_id,
            )

        candidates.sort(key=lambda c: c["pixel_x"])
        series_results.append({
            "series_id": series_id,
            "points": candidates,
        })

    return {"chart_id": chart_id, "series": series_results}


def validate_points_result(result: Dict[str, Any], truth: Dict[str, Any],
                           cfg: Dict[str, Any]) -> None:
    """Bounds re-check, executed after any input-side fault injection."""
    height, width = truth["canvas_shape"]
    for series in result["series"]:
        for cand in series["points"]:
            px, py = cand["pixel_x"], cand["pixel_y"]
            if not (0 <= px <= width - 1) or not (0 <= py <= height - 1):
                raise BusinessError(
                    codes.E_POINT_OUT_OF_CANVAS,
                    f"candidate ({px}, {py}) outside canvas {width}x{height}",
                    chart_id=result["chart_id"], stage=STAGE,
                    series_id=series["series_id"],
                    detail={"pixel_x": px, "pixel_y": py},
                )
