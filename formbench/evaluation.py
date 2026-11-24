from __future__ import annotations

from .detect import DetectedBlock
from .schema import TruthBlock


def iou_bbox(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    intersection = ix * iy
    union = aw * ah + bw * bh - intersection
    return 0.0 if union <= 0 else intersection / union


def match_to_truth(
    detected: list[DetectedBlock], truth: list[TruthBlock]
) -> dict[int, tuple[TruthBlock | None, float]]:
    pairs: list[tuple[float, int, int]] = []
    for d_index, block in enumerate(detected):
        for t_index, truth_block in enumerate(truth):
            score = iou_bbox(block.bbox, truth_block.bbox)
            if score > 0:
                pairs.append((score, d_index, t_index))
    pairs.sort(key=lambda item: (-item[0], item[1], item[2]))

    used_detected: set[int] = set()
    used_truth: set[int] = set()
    mapping: dict[int, tuple[TruthBlock | None, float]] = {
        block.block_index: (None, 0.0) for block in detected
    }
    for score, d_index, t_index in pairs:
        if d_index in used_detected or t_index in used_truth:
            continue
        used_detected.add(d_index)
        used_truth.add(t_index)
        mapping[d_index] = (truth[t_index], score)
    return mapping
