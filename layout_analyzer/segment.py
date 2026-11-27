from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .imageops import Foreground, projection_profiles


@dataclass
class MergedBlock:
    native_bbox: tuple[int, int, int, int]
    component_ids: list[int]
    area: int
    confidence: float
    merge_reason: dict[str, Any]


def segment_page(foreground: Foreground, scale: float) -> tuple[list[MergedBlock], dict[str, Any]]:
    components = foreground.components
    _, _, projection = projection_profiles(foreground.mask, scale)
    if not components:
        return [], {**projection, **foreground.diagnostics, "merge_thresholds": {}}

    parent = list(range(len(components)))

    def find(value: int) -> int:
        root = value
        while parent[root] != root:
            root = parent[root]
        while parent[value] != value:
            nxt = parent[value]
            parent[value] = root
            value = nxt
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    hgap = int(round(24 * scale))
    vgap = int(round(72 * scale))
    min_valley_width = max(2, int(round(30 * scale)))
    min_y_valley_width = max(2, int(round(40 * scale)))
    merges: list[dict[str, Any]] = []
    valleys = projection["x_valleys"]
    y_valleys = projection["y_valleys"]

    for i in range(len(components)):
        a = components[i]
        for j in range(i + 1, len(components)):
            b = components[j]
            x_gap, y_gap, x_overlap, y_overlap = _gaps(a.bbox, b.bbox)
            height_ratio = y_overlap / max(1, min(_height(a.bbox), _height(b.bbox)))
            width_ratio = x_overlap / max(1, min(_width(a.bbox), _width(b.bbox)))

            horizontal = (
                x_gap <= hgap
                and height_ratio >= 0.52
                and not _major_valley_between(a.bbox, b.bbox, valleys, min_valley_width)
            )
            vertical = (
                y_gap <= vgap
                and width_ratio >= 0.24
                and not _major_y_valley_between(
                    a.bbox, b.bbox, y_valleys, min_y_valley_width
                )
            )
            if horizontal or vertical:
                union(i, j)
                merges.append(
                    {
                        "a": i,
                        "b": j,
                        "direction": "horizontal" if horizontal else "vertical",
                        "x_gap_px": x_gap,
                        "y_gap_px": y_gap,
                    }
                )

    groups: dict[int, list[int]] = {}
    for index in range(len(components)):
        groups.setdefault(find(index), []).append(index)

    blocks: list[MergedBlock] = []
    min_block_ink = max(20, int(round(70 * scale * scale)))
    for root, member_indices in sorted(groups.items()):
        members = [components[index] for index in member_indices]
        bbox = (
            min(member.bbox[0] for member in members),
            min(member.bbox[1] for member in members),
            max(member.bbox[2] for member in members),
            max(member.bbox[3] for member in members),
        )
        area = sum(member.area for member in members)
        if area < min_block_ink:
            continue
        if _width(bbox) < max(4, int(round(5 * scale))) or _height(bbox) < max(4, int(round(5 * scale))):
            continue
        component_count = len(members)
        confidence = min(
            0.97,
            0.58
            + 0.10 * math.log2(component_count + 1)
            + min(0.15, area / max(1.0, 4000.0 * scale * scale)),
        )
        blocks.append(
            MergedBlock(
                native_bbox=bbox,
                component_ids=[member.cid for member in members],
                area=area,
                confidence=round(confidence, 4),
                merge_reason={
                    "fragment_count": component_count,
                    "merged_by": _group_merge_directions(member_indices, merges),
                    "projection_valley_respected": True,
                },
            )
        )

    thresholds = {
        "horizontal_char_gap_px": hgap,
        "vertical_line_gap_px": vgap,
        "minimum_projection_valley_px": min_valley_width,
        "minimum_y_projection_valley_px": min_y_valley_width,
        "minimum_block_ink_px": min_block_ink,
    }
    diagnostics = {
        **projection,
        **foreground.diagnostics,
        "merge_thresholds": thresholds,
        "atomic_component_count": len(components),
        "candidate_block_count": len(blocks),
        "successful_merge_pairs": len(merges),
    }
    return blocks, diagnostics


def _group_merge_directions(member_indices: list[int], merges: list[dict[str, Any]]) -> list[str]:
    member_set = set(member_indices)
    directions = {
        merge["direction"]
        for merge in merges
        if merge["a"] in member_set and merge["b"] in member_set
    }
    return sorted(directions)


def _gaps(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    x_gap = max(0, bx0 - ax1, ax0 - bx1)
    y_gap = max(0, by0 - ay1, ay0 - by1)
    x_overlap = max(0, min(ax1, bx1) - max(ax0, bx0))
    y_overlap = max(0, min(ay1, by1) - max(ay0, by0))
    return x_gap, y_gap, x_overlap, y_overlap


def _major_valley_between(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int],
    valleys: list[list[int]],
    min_width: int,
) -> bool:
    ax0, _, ax1, _ = a
    bx0, _, bx1, _ = b
    if ax0 <= bx0:
        left, right = ax1, bx0
    else:
        left, right = bx1, ax0
    if right <= left:
        return False
    for start, end in valleys:
        if end - start < min_width:
            continue
        if end > left and start < right:
            return True
    return False


def _major_y_valley_between(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int],
    valleys: list[list[int]],
    min_width: int,
) -> bool:
    _, ay0, _, ay1 = a
    _, by0, _, by1 = b
    if ay0 <= by0:
        top, bottom = ay1, by0
    else:
        top, bottom = by1, ay0
    if bottom <= top:
        return False
    for start, end in valleys:
        if end - start < min_width:
            continue
        if end > top and start < bottom:
            return True
    return False


def _width(bbox: tuple[int, int, int, int]) -> int:
    return bbox[2] - bbox[0]


def _height(bbox: tuple[int, int, int, int]) -> int:
    return bbox[3] - bbox[1]
