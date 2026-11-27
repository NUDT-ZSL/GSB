"""Back-light rim continuity diagnostics.

The rim guard fibres form two vertical chains along the left and right bottle
silhouette.  We project those chains, walk them in screen-row order and flag
every row whose coverage drops below the continuity threshold, together with
the fibres responsible for the neighbouring segments.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np

from .models import Curve, PackagingProject
from .rasterizer import RasterResult, project_curve


@dataclass
class ContourBreak:
    side: str
    row: int
    x: int
    coverage: float
    neighbors: List[float]
    curve_ids: List[str]


@dataclass
class ContourReport:
    chains: Dict[str, List[Tuple[int, int]]]
    breaks: List[ContourBreak]
    threshold: float
    rows_checked: int


def _rim_segments(project: PackagingProject
                  ) -> Dict[str, List[Tuple[Curve, np.ndarray]]]:
    groups: Dict[str, List] = {"left": [], "right": []}
    for curve in project.valid_curves():
        if not curve.rim_guard:
            continue
        xy, _depth = project_curve(curve, project.camera)
        if len(xy) == 0:
            continue
        side = "left" if xy[:, 0].mean() < project.camera.width_px * 0.5 else "right"
        groups[side].append((curve, xy))
    for side in groups:
        groups[side].sort(key=lambda item: item[1][:, 1].mean())
    return groups

def check_continuity(project: PackagingProject, result: RasterResult,
                     threshold: float = 0.05, band: int = 1) -> ContourReport:
    groups = _rim_segments(project)
    coverage = result.coverage
    height, width = coverage.shape
    contributions = result.contributions
    breaks: List[ContourBreak] = []
    chains: Dict[str, List[Tuple[int, int]]] = {}
    rows_checked = 0

    for side, segments in groups.items():
        chain_pixels: List[Tuple[int, int]] = []
        chain_top = min(int(round(float(xy[:, 1].min()))) for _curve, xy in segments)
        chain_bottom = max(int(round(float(xy[:, 1].max()))) for _curve, xy in segments)
        for row in range(max(chain_top, 0), min(chain_bottom + 1, height)):
            x = int(round(float(np.mean([xy[:, 0].mean() for _c, xy in segments]))))
            x = min(max(x, 0), width - 1)
            chain_pixels.append((x, row))
            rows_checked += 1
            window = coverage[
                max(row - band, 0):min(row + band + 1, height),
                max(x - band, 0):min(x + band + 1, width)]
            value = float(coverage[row, x])
            if value < threshold and float(window.max()) < threshold:
                neighbor_values = [
                    float(coverage[min(max(row + delta, 0), height - 1), x])
                    for delta in (-2, -1, 1, 2)
                ]
                nearby = contributions[
                    (np.abs(contributions["x"] - x) <= 2)
                    & (np.abs(contributions["y"] - row) <= 3)
                ] if len(contributions) else contributions
                ids = sorted({str(row_rec["curve_id"]) for row_rec in nearby
                              if str(row_rec["curve_id"]).startswith("RM-")})
                owner_index = next(
                    (idx for idx, (_c, xy_c) in enumerate(segments)
                     if round(float(xy_c[:, 1].min())) - 3 <= row
                     <= round(float(xy_c[:, 1].max())) + 3),
                    None,
                )
                neighbours_ids = []
                if owner_index is not None:
                    if owner_index > 0:
                        neighbours_ids.append(segments[owner_index - 1][0].curve_id)
                    if owner_index + 1 < len(segments):
                        neighbours_ids.append(segments[owner_index + 1][0].curve_id)
                if not neighbours_ids:
                    nearest = sorted(
                        segments,
                        key=lambda item: abs(float(item[1][:, 1].mean()) - row))[:2]
                    neighbours_ids = [item[0].curve_id for item in nearest]
                breaks.append(ContourBreak(
                    side=side, row=row, x=x, coverage=value,
                    neighbors=neighbor_values,
                    curve_ids=sorted(set(neighbours_ids or ids)) + ["missing-link"],
                ))
        chains[side] = chain_pixels

    return ContourReport(
        chains=chains, breaks=breaks, threshold=threshold,
        rows_checked=rows_checked,
    )


def report_to_dict(report: ContourReport) -> Dict:
    return {
        "threshold": report.threshold,
        "rows_checked": report.rows_checked,
        "chain_pixels": {
            side: pixels[:60] for side, pixels in report.chains.items()
        },
        "break_count": len(report.breaks),
        "breaks": [
            {
                "side": item.side,
                "row": item.row,
                "x": item.x,
                "coverage": item.coverage,
                "neighbors": item.neighbors,
                "curve_ids": item.curve_ids,
            }
            for item in report.breaks
        ],
    }
