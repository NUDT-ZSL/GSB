"""本地确定性版面样本生成器。

固定画布（1240x1754，A4@150dpi）、固定种子、四类版面族：

* ``single_column`` 单栏报告；
* ``two_column``    双栏论文；
* ``three_column``  三栏杂志；
* ``mixed_regions`` 跨区混合（标题通栏 + 双栏 + 图注 + 页眉页脚）。

每个块带像素级外接矩形、类型、文本长度与真值 ``reading_index``；输出前对块
列表做种子控制的确定性洗牌，使 block_id 顺序不泄漏真值阅读顺序。
"""

from __future__ import annotations

import json
import os

import numpy as np

from .geometry import Page
from .snapshots import canonical_json

CANVAS_HEIGHT = 1754
CANVAS_WIDTH = 1240
MARGIN_X = 90
MARGIN_TOP = 110
MARGIN_BOTTOM = 110
GAP = 26

FAMILIES = ("single_column", "two_column", "three_column", "mixed_regions")


def _new_rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(abs(int(seed)) + 7919)


def _emit(
    page_id: str,
    seed: int,
    family: str,
    rects: list[tuple[int, int, int, int, str]],
) -> dict:
    """rects 按阅读顺序给出 (x1,y1,x2,y2,type)，据此赋真值并确定性洗牌。"""
    rng = _new_rng(seed)
    order_indices = list(range(len(rects)))
    rng.shuffle(order_indices)
    blocks = []
    for shuffled_pos, truth_index in enumerate(order_indices):
        x1, y1, x2, y2, block_type = rects[truth_index]
        text_rng = np.random.default_rng(seed * 100003 + truth_index * 97 + 17)
        if block_type in ("header", "footer"):
            text_len = int(text_rng.integers(20, 80))
        elif block_type == "title":
            text_len = int(text_rng.integers(40, 160))
        elif block_type == "caption":
            text_len = int(text_rng.integers(30, 120))
        else:
            text_len = int(text_rng.integers(120, 900))
        blocks.append(
            {
                "block_id": f"b{shuffled_pos:04d}",
                "x1": int(x1),
                "y1": int(y1),
                "x2": int(x2),
                "y2": int(y2),
                "block_type": block_type,
                "text_len": text_len,
                "reading_index": truth_index,
            }
        )
    blocks.sort(key=lambda item: item["block_id"])
    return {
        "page_id": page_id,
        "canvas_shape": [CANVAS_HEIGHT, CANVAS_WIDTH],
        "layout_family": family,
        "seed": int(seed),
        "blocks": blocks,
    }


def _column_rects(
    n_body: int,
    columns: list[tuple[int, int]],
    rng: np.random.Generator,
    top: int,
    bottom: int,
    caption_every: int,
) -> list[tuple[int, int, int, int, str]]:
    """在给定栏中自上而下放置正文块，周期性插图注；返回顺序即阅读顺序。"""
    per_column = [n_body // len(columns)] * len(columns)
    for idx in range(n_body % len(columns)):
        per_column[idx] += 1
    rects: list[tuple[int, int, int, str]] = []
    usable_height = bottom - top
    for column_idx, (x1, x2) in enumerate(columns):
        count = per_column[column_idx]
        if count == 0:
            continue
        heights = rng.integers(70, 150, size=count)
        total_gap = GAP * (count - 1)
        scale = (usable_height - total_gap) / max(int(heights.sum()), 1)
        heights = np.maximum(28, np.floor(heights * scale).astype(int))
        # 缩放后仍可能因取整超出可用高度，按差额从最高块逐档削减。
        while int(heights.sum()) + total_gap > usable_height:
            tallest = int(np.argmax(heights))
            heights[tallest] -= 1
        y = top
        for seq, height in enumerate(heights):
            block_type = "caption" if caption_every and seq and seq % caption_every == 0 else "text"
            rects.append((x1, int(y), x2, int(y + int(height)), block_type))
            y += int(height) + GAP
    return rects


def generate_single_column(n_blocks: int, seed: int, page_id: str) -> dict:
    rng = _new_rng(seed + 11)
    rects: list[tuple[int, int, int, int, str]] = []
    y = MARGIN_TOP
    rects.append((MARGIN_X, y, CANVAS_WIDTH - MARGIN_X, y + 60, "header"))
    y += 60 + GAP
    title_bottom = y + 90
    rects.append((MARGIN_X, y, CANVAS_WIDTH - MARGIN_X, title_bottom, "title"))
    y = title_bottom + GAP
    body_count = max(1, n_blocks - 3)
    body = _column_rects(
        body_count,
        [(MARGIN_X, CANVAS_WIDTH - MARGIN_X)],
        rng,
        y,
        CANVAS_HEIGHT - MARGIN_BOTTOM - 70,
        caption_every=4,
    )
    rects.extend(body)
    footer_y = CANVAS_HEIGHT - MARGIN_BOTTOM
    rects.append((MARGIN_X, footer_y, CANVAS_WIDTH - MARGIN_X, footer_y + 50, "footer"))
    return _emit(page_id, seed, "single_column", rects)


def generate_two_column(n_blocks: int, seed: int, page_id: str) -> dict:
    rng = _new_rng(seed + 23)
    rects: list[tuple[int, int, int, int, str]] = []
    y = MARGIN_TOP
    rects.append((MARGIN_X, y, CANVAS_WIDTH - MARGIN_X, y + 55, "header"))
    y += 55 + GAP
    title_bottom = y + 80
    rects.append((MARGIN_X, y, CANVAS_WIDTH - MARGIN_X, title_bottom, "title"))
    body_top = title_bottom + GAP
    body_bottom = CANVAS_HEIGHT - MARGIN_BOTTOM - 70
    mid = CANVAS_WIDTH // 2
    columns = [
        (MARGIN_X, mid - GAP // 2),
        (mid + GAP // 2, CANVAS_WIDTH - MARGIN_X),
    ]
    body_count = max(2, n_blocks - 3)
    rects.extend(
        _column_rects(body_count, columns, rng, body_top, body_bottom, caption_every=3)
    )
    footer_y = CANVAS_HEIGHT - MARGIN_BOTTOM
    rects.append((MARGIN_X, footer_y, CANVAS_WIDTH - MARGIN_X, footer_y + 50, "footer"))
    return _emit(page_id, seed, "two_column", rects)


def generate_three_column(n_blocks: int, seed: int, page_id: str) -> dict:
    rng = _new_rng(seed + 37)
    rects: list[tuple[int, int, int, int, str]] = []
    y = MARGIN_TOP
    rects.append((MARGIN_X, y, CANVAS_WIDTH - MARGIN_X, y + 50, "header"))
    y += 50 + GAP
    body_top = y
    body_bottom = CANVAS_HEIGHT - MARGIN_BOTTOM - 70
    usable_width = CANVAS_WIDTH - 2 * MARGIN_X - 2 * GAP
    column_width = usable_width // 3
    columns = []
    for idx in range(3):
        x1 = MARGIN_X + idx * (column_width + GAP)
        columns.append((x1, x1 + column_width))
    body_count = max(3, n_blocks - 2)
    rects.extend(
        _column_rects(body_count, columns, rng, body_top, body_bottom, caption_every=3)
    )
    footer_y = CANVAS_HEIGHT - MARGIN_BOTTOM
    rects.append((MARGIN_X, footer_y, CANVAS_WIDTH - MARGIN_X, footer_y + 50, "footer"))
    return _emit(page_id, seed, "three_column", rects)


def generate_mixed_regions(n_blocks: int, seed: int, page_id: str) -> dict:
    rng = _new_rng(seed + 53)
    rects: list[tuple[int, int, int, int, str]] = []
    y = MARGIN_TOP
    rects.append((MARGIN_X, y, CANVAS_WIDTH - MARGIN_X, y + 55, "header"))
    y += 55 + GAP
    title_bottom = y + 85
    rects.append((MARGIN_X, y, CANVAS_WIDTH - MARGIN_X, title_bottom, "title"))
    y = title_bottom + GAP

    body_bottom = CANVAS_HEIGHT - MARGIN_BOTTOM - 130
    mid = CANVAS_WIDTH // 2
    columns = [
        (MARGIN_X, mid - GAP // 2),
        (mid + GAP // 2, CANVAS_WIDTH - MARGIN_X),
    ]
    # 调用方请求的 n_blocks 含 header/title/footer/通栏图注共 4 个固定块。
    body_count = max(1, n_blocks - 4)
    body = _column_rects(body_count, columns, rng, y, body_bottom, caption_every=2)
    rects.extend(body)

    caption_y = body_bottom + GAP
    rects.append(
        (MARGIN_X, caption_y, CANVAS_WIDTH - MARGIN_X, caption_y + 60, "caption")
    )
    footer_y = CANVAS_HEIGHT - MARGIN_BOTTOM
    rects.append((MARGIN_X, footer_y, CANVAS_WIDTH - MARGIN_X, footer_y + 50, "footer"))
    return _emit(page_id, seed, "mixed_regions", rects)


GENERATORS = {
    "single_column": generate_single_column,
    "two_column": generate_two_column,
    "three_column": generate_three_column,
    "mixed_regions": generate_mixed_regions,
}


def generate_page(family: str, n_blocks: int, seed: int, page_id: str) -> dict:
    if family not in GENERATORS:
        raise ValueError(f"未知版面族: {family}")
    return GENERATORS[family](n_blocks, seed, page_id)


def baseline_specs() -> list[dict]:
    """完整基线：覆盖小/中/大规模与四种版面族。"""
    return [
        {"page_id": "page_s01", "family": "single_column", "n_blocks": 8, "seed": 1001},
        {"page_id": "page_s02", "family": "two_column", "n_blocks": 12, "seed": 1002},
        {"page_id": "page_m01", "family": "two_column", "n_blocks": 22, "seed": 2001},
        {"page_id": "page_m02", "family": "mixed_regions", "n_blocks": 30, "seed": 2002},
        {"page_id": "page_l01", "family": "three_column", "n_blocks": 36, "seed": 3001},
    ]


def scale_sweep_specs() -> list[dict]:
    """一次规模变化追踪：块数单调上升，穿过 exact -> banded -> greedy 阈值。"""
    families = [
        "single_column",
        "two_column",
        "two_column",
        "mixed_regions",
        "three_column",
        "three_column",
        "three_column",
    ]
    sizes = [7, 11, 16, 26, 36, 48, 60]
    return [
        {
            "page_id": f"sweep_{idx:02d}_n{size}",
            "family": families[idx],
            "n_blocks": size,
            "seed": 4000 + idx,
        }
        for idx, size in enumerate(sizes)
    ]


def write_jsonl(specs: list[dict], path: str) -> list[dict]:
    records = []
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for spec in specs:
            record = generate_page(
                spec["family"], spec["n_blocks"], spec["seed"], spec["page_id"]
            )
            records.append(record)
            handle.write(canonical_json(record))
            handle.write("\n")
    return records


def generate_baseline_dataset(path: str) -> list[dict]:
    return write_jsonl(baseline_specs(), path)


def generate_scale_sweep_dataset(path: str) -> list[dict]:
    return write_jsonl(scale_sweep_specs(), path)


def pages_from_records(records: list[dict]) -> list[Page]:
    from .geometry import parse_page

    return [parse_page(record) for record in records]
