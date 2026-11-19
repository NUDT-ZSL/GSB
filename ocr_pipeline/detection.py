"""Stage 1: deterministic text-line detection.

The detector first binarizes the page, then locates occupied row bands via the
horizontal projection.  Connected components inside each band are merged with
deterministic rules into candidate line boxes.  Tie-breaking is always
(y ascending, x ascending) and coordinates are integer pixels.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .templates import GLYPH_HEIGHT, GLYPH_WIDTH

from .sample import GLYPH_GAP, LINE_GAP, MARGIN_Y

from .util import array_hash

INK_THRESHOLD = 128
MIN_COMPONENT_PIXELS = 3
# Glyph slots are separated by a 2-column blank gap; any gap up to the glyph
# width still belongs to the same text line and is merged deterministically.
COMPONENT_MERGE_GAP = 10


@dataclass(frozen=True)
class DetectedLine:
    line_no: int
    box: tuple[int, int, int, int]

    def to_json(self) -> dict:
        return {"line_no": self.line_no, "box": list(self.box)}


def _connected_components(mask: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    """4-neighbour flood fill; returns (x1,y1,x2,y2,area) sorted deterministically."""
    height, width = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    components: list[tuple[int, int, int, int, int]] = []
    for start_y in range(height):
        for start_x in range(width):
            if not mask[start_y, start_x] or visited[start_y, start_x]:
                continue
            stack = [(start_y, start_x)]
            visited[start_y, start_x] = True
            min_x = max_x = start_x
            min_y = max_y = start_y
            area = 0
            while stack:
                y, x = stack.pop()
                area += 1
                min_x = min(min_x, x)
                max_x = max(max_x, x)
                min_y = min(min_y, y)
                max_y = max(max_y, y)
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if (
                        0 <= ny < height
                        and 0 <= nx < width
                        and mask[ny, nx]
                        and not visited[ny, nx]
                    ):
                        visited[ny, nx] = True
                        stack.append((ny, nx))
            components.append((min_x, min_y, max_x, max_y, area))
    # Explicit deterministic ordering independent of set/dict iteration.
    components.sort(key=lambda c: (c[1], c[0], c[2], c[3], c[4]))
    return components


def _row_bands(binary: np.ndarray) -> list[tuple[int, int]]:
    row_dark = (binary > 0).sum(axis=1)
    bands: list[tuple[int, int]] = []
    start: int | None = None
    for y, count in enumerate(row_dark):
        if count > 0 and start is None:
            start = y
        elif count == 0 and start is not None:
            bands.append((start, y - 1))
            start = None
    if start is not None:
        bands.append((start, len(row_dark) - 1))
    return bands


def detect_lines(image: np.ndarray) -> list[DetectedLine]:
    binary = (image < INK_THRESHOLD).astype(np.uint8)
    detections: list[DetectedLine] = []
    line_no = 0
    for band_y1, band_y2 in _row_bands(binary):
        band = binary[band_y1:band_y2 + 1, :]
        components = [
            component
            for component in _connected_components(band)
            if component[4] >= MIN_COMPONENT_PIXELS
        ]
        if not components:
            continue
        # Merge components whose horizontal gap is within the rule threshold.
        ordered = sorted(components, key=lambda c: (c[0], c[1]))
        merged: list[list[int]] = []
        for x1, y1, x2, y2, _area in ordered:
            if not merged or x1 - merged[-1][2] > COMPONENT_MERGE_GAP:
                merged.append([x1, y1, x2, y2])
            else:
                merged[-1][0] = min(merged[-1][0], x1)
                merged[-1][1] = min(merged[-1][1], y1)
                merged[-1][2] = max(merged[-1][2], x2)
                merged[-1][3] = max(merged[-1][3], y2)
        boxes = []
        for x1, y1, x2, y2 in merged:
            page_width = binary.shape[1]
            # Snap the band to the fixed line grid so isolated salt pixels
            # above/below the true ink cannot shift the detected origin.
            raw_y1 = band_y1 + y1
            grid_index = max(0, round((raw_y1 - MARGIN_Y) / (GLYPH_HEIGHT + LINE_GAP)))
            normalized_y1 = MARGIN_Y + grid_index * (GLYPH_HEIGHT + LINE_GAP)
            normalized_y2 = normalized_y1 + GLYPH_HEIGHT - 1
            normalized_x1 = max(0, x1)
            # Regularize width to whole glyph slots (each slot is glyph+gap).
            span = x2 - x1 + 1
            slot_count = max(1, round(span / (GLYPH_WIDTH + GLYPH_GAP)))
            normalized_x2 = min(
                page_width - 1, normalized_x1 + slot_count * (GLYPH_WIDTH + GLYPH_GAP) - GLYPH_GAP - 1
            )
            boxes.append((normalized_x1, normalized_y1, normalized_x2, normalized_y2))
        # Required tie-break: y ascending, then x ascending.
        boxes.sort(key=lambda box: (box[1], box[0]))
        for box in boxes:
            detections.append(DetectedLine(line_no=line_no, box=box))
            line_no += 1
    return detections


def boxes_summary(detections: list[DetectedLine]) -> str:
    payload = [list(detection.box) for detection in detections]
    return array_hash(np.asarray(payload, dtype=np.int64)) if payload else "empty"


def detection_stats(
    detections: list[DetectedLine],
    truth: list,
) -> dict:
    """Ground-truth coverage stats, independently recomputable from boxes."""
    detected_boxes = [detection.box for detection in detections]
    covered = 0
    per_truth: list[dict] = []
    for gt in truth:
        best_iou = max(
            (_iou(gt.box, candidate) for candidate in detected_boxes),
            default=0.0,
        )
        is_covered = best_iou >= 0.5
        covered += int(is_covered)
        per_truth.append(
            {
                "line_no": gt.line_no,
                "best_iou": round(float(best_iou), 6),
                "covered": is_covered,
            }
        )
    total = len(truth)
    return {
        "detected_lines": len(detected_boxes),
        "truth_lines": total,
        "covered_truth_lines": covered,
        "coverage": round(covered / total, 6) if total else 1.0,
        "per_truth": sorted(per_truth, key=lambda item: item["line_no"]),
    }


def _iou(box_a: tuple[int, ...], box_b: tuple[int, ...]) -> float:
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1 + 1), max(0, iy2 - iy1 + 1)
    inter = iw * ih
    area_a = max(0, ax2 - ax1 + 1) * max(0, ay2 - ay1 + 1)
    area_b = max(0, bx2 - bx1 + 1) * max(0, by2 - by1 + 1)
    union = area_a + area_b - inter
    return inter / union if union else 0.0
