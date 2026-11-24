from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class DetectedBlock:
    block_index: int
    x: int
    y: int
    width: int
    height: int
    density: float

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.width, self.height

    def to_dict(self) -> dict[str, object]:
        return {
            "block_index": self.block_index,
            "bbox": list(self.bbox),
            "density": round(self.density, 6),
        }


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def _row_runs(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    runs: list[tuple[int, int, int, int]] = []
    for y in range(mask.shape[0]):
        xs = np.flatnonzero(mask[y])
        if xs.size == 0:
            continue
        breaks = np.flatnonzero(np.diff(xs) > 1)
        starts = np.r_[xs[0], xs[breaks + 1]]
        ends = np.r_[xs[breaks], xs[-1]]
        for x0, x1 in zip(starts.tolist(), ends.tolist()):
            runs.append((y, y + 1, int(x0), int(x1) + 1))
    return runs


def _component_boxes(mask: np.ndarray, min_area: int) -> list[tuple[int, int, int, int, int]]:
    runs = _row_runs(mask)
    if not runs:
        return []
    uf = _UnionFind(len(runs))
    previous_row: list[int] = []
    current_row: list[int] = []
    current_y = runs[0][0]
    for index, (_, y, x0, x1) in enumerate(runs):
        y0, y1, x0, x1 = runs[index]
        if y0 != current_y:
            previous_row = current_row
            current_row = []
            current_y = y0
        current_row.append(index)
        for prev_index in previous_row:
            _, prev_y1, p0, p1 = runs[prev_index]
            if prev_y1 != y0:
                continue
            if x0 <= p1 + 1 and x1 >= p0 - 1:
                uf.union(index, prev_index)

    boxes: dict[int, list[int]] = {}
    areas: dict[int, int] = {}
    for index, (y0, y1, x0, x1) in enumerate(runs):
        root = uf.find(index)
        item = boxes.get(root)
        area = x1 - x0
        if item is None:
            boxes[root] = [x0, y0, x1, y1]
            areas[root] = area
        else:
            item[0] = min(item[0], x0)
            item[1] = min(item[1], y0)
            item[2] = max(item[2], x1)
            item[3] = max(item[3], y1)
            areas[root] += area

    result = []
    for root, box in boxes.items():
        x0, y0, x1, y1 = box
        area = areas[root]
        if area >= min_area:
            result.append((x0, y0, x1 - x0, y1 - y0, area))
    result.sort(key=lambda item: (item[1], item[0], item[3], item[2]))
    return result


def _line_bands(fraction: np.ndarray, threshold: float) -> list[tuple[int, int]]:
    indices = np.flatnonzero(fraction >= threshold)
    if indices.size == 0:
        return []
    bands: list[tuple[int, int]] = []
    start = int(indices[0])
    previous = start
    for value in indices[1:].tolist():
        if value - previous > 2:
            bands.append((start, previous + 1))
            start = value
        previous = value
    bands.append((start, previous + 1))
    return bands


def suppress_table_lines(
    image: np.ndarray,
    *,
    ink_cutoff: int = 175,
    line_fraction: float = 0.45,
    expansion: int = 3,
) -> tuple[np.ndarray, dict[str, list[tuple[int, int]]]]:
    dark = image < ink_cutoff
    row_fraction = dark.mean(axis=1)
    col_fraction = dark.mean(axis=0)
    horizontal = _line_bands(row_fraction, line_fraction)
    vertical = _line_bands(col_fraction, line_fraction)
    text_mask = dark.copy()
    for y0, y1 in horizontal:
        text_mask[max(0, y0 - expansion) : min(image.shape[0], y1 + expansion), :] = False
    for x0, x1 in vertical:
        text_mask[:, max(0, x0 - expansion) : min(image.shape[1], x1 + expansion)] = False
    return text_mask, {"horizontal": horizontal, "vertical": vertical}


def detect_blocks(
    image: np.ndarray,
    *,
    detection_threshold: float = 0.12,
    ink_cutoff: int = 175,
    min_component_area: int = 18,
    merge_gap: int | None = None,
) -> list[DetectedBlock]:
    """Detect whole-coordinate text rectangles without using the chunk grid."""
    text_mask, lines = suppress_table_lines(image, ink_cutoff=ink_cutoff)
    components = _component_boxes(text_mask, min_area=min_component_area)
    if not components:
        return []

    heights = np.asarray([item[3] for item in components], dtype=np.float32)
    median_height = max(8.0, float(np.median(heights)))
    gap = merge_gap if merge_gap is not None else int(round(median_height * 0.85)) + 14
    vertical_tolerance = max(8, int(round(median_height * 0.45)))

    count = len(components)
    uf = _UnionFind(count)
    for i, left in enumerate(components):
        x0, y0, w, h, _ = left
        x1 = x0 + w
        y1 = y0 + h
        for j in range(i + 1, count):
            rx, ry, rw, rh, _ = components[j]
            rx1 = rx + rw
            ry1 = ry + rh
            horizontal_gap = max(0, max(x0, rx) - min(x1, rx1))
            vertical_gap = max(0, max(y0, ry) - min(y1, ry1))
            vertical_overlap = min(y1, ry1) - max(y0, ry)
            vertically_compatible = (
                vertical_overlap >= min(h, rh) * 0.35
                or vertical_gap <= vertical_tolerance
            )
            if horizontal_gap <= gap and vertically_compatible:
                uf.union(i, j)

    groups: dict[int, list[tuple[int, int, int, int, int]]] = {}
    for index, component in enumerate(components):
        groups.setdefault(uf.find(index), []).append(component)

    detected: list[DetectedBlock] = []
    for group in groups.values():
        x0 = min(item[0] for item in group)
        y0 = min(item[1] for item in group)
        x1 = max(item[0] + item[2] for item in group)
        y1 = max(item[1] + item[3] for item in group)
        width = x1 - x0
        height = y1 - y0
        pixel_area = sum(item[4] for item in group)
        density = pixel_area / float(max(1, width * height))
        if width < 8 or height < 10 or height > image.shape[0] // 2:
            continue
        if density < detection_threshold:
            continue
        detected.append(
            DetectedBlock(
                block_index=-1,
                x=int(x0),
                y=int(y0),
                width=int(width),
                height=int(height),
                density=float(density),
            )
        )

    detected.sort(key=lambda block: (block.x, block.y, block.width, block.height))
    return [
        DetectedBlock(
            block_index=index,
            x=block.x,
            y=block.y,
            width=block.width,
            height=block.height,
            density=block.density,
        )
        for index, block in enumerate(detected)
    ]
