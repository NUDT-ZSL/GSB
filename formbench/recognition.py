from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .glyphs import Template, normalize_text


@dataclass(frozen=True)
class NormalizationConfig:
    target_cap_height: int = 40
    template_cell: int = 8
    ink_cutoff: int = 175
    max_shift: int = 3


@dataclass
class NormalizedBlock:
    block_index: int
    bitmap: np.ndarray
    scale: float
    source_width: int
    source_height: int

    @property
    def buffer_bytes(self) -> int:
        return int(self.bitmap.nbytes)


@dataclass(frozen=True)
class RecognitionResult:
    block_index: int
    text: str
    confidence: float
    score: float
    runner_up: str
    runner_up_score: float
    scale: float


def global_scale(heights: list[int] | np.ndarray, config: NormalizationConfig) -> float:
    if len(heights) == 0:
        return 1.0
    median_height = float(np.median(np.asarray(heights, dtype=np.float64)))
    if median_height <= 0:
        return 1.0
    scale = config.target_cap_height / median_height
    return float(min(2.0, max(0.5, scale)))


def crop_pixels(image: np.ndarray, bbox: tuple[int, int, int, int]) -> np.ndarray:
    x, y, w, h = bbox
    return np.ascontiguousarray(image[y : y + h, x : x + w]).copy()


def normalize_block(
    block_index: int,
    crop: np.ndarray,
    scale: float,
    config: NormalizationConfig,
) -> NormalizedBlock:
    height, width = crop.shape
    new_height = max(1, int(round(height * scale)))
    new_width = max(1, int(round(width * scale)))
    y_index = np.minimum(
        new_height - 1,
        np.floor(np.arange(new_height, dtype=np.float64) * height / max(1, new_height)).astype(int),
    )
    x_index = np.minimum(
        new_width - 1,
        np.floor(np.arange(new_width, dtype=np.float64) * width / max(1, new_width)).astype(int),
    )
    resized = crop[np.ix_(y_index, x_index)]
    bitmap = (resized < config.ink_cutoff).astype(np.uint8)
    return NormalizedBlock(
        block_index=block_index,
        bitmap=bitmap,
        scale=scale,
        source_width=width,
        source_height=height,
    )


def _shifted_overlap_score(
    block: np.ndarray, template: np.ndarray, max_shift: int
) -> tuple[float, int, int]:
    bh, bw = block.shape
    th, tw = template.shape
    pad_h = max(bh, th) + 2 * max_shift + 4
    pad_w = max(bw, tw) + 2 * max_shift + 8

    def place(bitmap: np.ndarray, dy: int, dx: int) -> np.ndarray:
        canvas = np.zeros((pad_h, pad_w), dtype=np.uint8)
        y = (pad_h - bitmap.shape[0]) // 2 + dy
        x = (pad_w - bitmap.shape[1]) // 2 + dx
        source_y0 = max(0, -y)
        source_x0 = max(0, -x)
        target_y0 = max(0, y)
        target_x0 = max(0, x)
        copy_h = min(bitmap.shape[0] - source_y0, pad_h - target_y0)
        copy_w = min(bitmap.shape[1] - source_x0, pad_w - target_x0)
        if copy_h > 0 and copy_w > 0:
            canvas[
                target_y0 : target_y0 + copy_h,
                target_x0 : target_x0 + copy_w,
            ] = bitmap[
                source_y0 : source_y0 + copy_h,
                source_x0 : source_x0 + copy_w,
            ]
        return canvas

    template_canvas = place(template, 0, 0)
    template_sum = int(template.sum())
    best = (0.0, 0, 0)
    block_sum = int(block.sum())
    for dy in range(-max_shift, max_shift + 1):
        for dx in range(-max_shift, max_shift + 1):
            block_canvas = place(block, dy, dx)
            intersection = int(np.logical_and(block_canvas, template_canvas).sum())
            denominator = block_sum + template_sum
            if denominator == 0:
                score = 0.0
            else:
                score = 2.0 * intersection / denominator
            if score > best[0]:
                best = (score, dy, dx)
    return best


def recognize_bitmap(
    block_index: int,
    normalized: NormalizedBlock,
    templates: list[Template],
) -> RecognitionResult:
    if not templates:
        raise ValueError("template set is empty")
    scores_by_text: dict[str, float] = {}
    for template in templates:
        score, _, _ = _shifted_overlap_score(
            normalized.bitmap, template.bitmap, max_shift=3
        )
        previous = scores_by_text.get(template.text, -1.0)
        scores_by_text[template.text] = max(previous, score)
    ranked = sorted(
        ((score, text) for text, score in scores_by_text.items()),
        key=lambda item: (-item[0], item[1]),
    )
    top_score, top_text = ranked[0]
    runner_up_score, runner_up_text = ranked[1] if len(ranked) > 1 else (0.0, top_text)
    margin = max(0.0, top_score - runner_up_score)
    confidence = min(0.99, max(0.05, 0.22 + 0.70 * top_score + 1.20 * margin))
    confidence = round(min(0.99, confidence), 6)
    return RecognitionResult(
        block_index=block_index,
        text=normalize_text(top_text),
        confidence=confidence,
        score=round(top_score, 6),
        runner_up=normalize_text(runner_up_text),
        runner_up_score=round(runner_up_score, 6),
        scale=normalized.scale,
    )
