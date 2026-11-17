#!/usr/bin/env python3
from __future__ import annotations

import argparse
import binascii
import json
import random
import struct
import zlib
from pathlib import Path
from typing import Any

import numpy as np


PAGE_WIDTH = 720
PAGE_HEIGHT = 960
BACKGROUND = 248
BORDER = 35


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = binascii.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def write_gray_png(path: Path, image: np.ndarray) -> None:
    height, width = image.shape
    raw = bytearray()
    contiguous = np.ascontiguousarray(image, dtype=np.uint8)
    for row in contiguous:
        raw.append(0)
        raw.extend(row.tobytes())
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def draw_border(canvas: np.ndarray, bbox: tuple[int, int, int, int], value: int = BORDER) -> None:
    x, y, width, height = bbox
    canvas[y, x : x + width] = value
    canvas[y + height - 1, x : x + width] = value
    canvas[y : y + height, x] = value
    canvas[y : y + height, x + width - 1] = value


def draw_text_block(
    canvas: np.ndarray,
    bbox: tuple[int, int, int, int],
    rng: random.Random,
    *,
    kind: str,
) -> None:
    x, y, width, height = bbox
    draw_border(canvas, bbox)
    if kind == "title":
        line_thickness = 7
        pitch = 16
        pad_x = 9
    elif kind in {"header_footer"}:
        line_thickness = 3
        pitch = 10
        pad_x = 7
    else:
        line_thickness = 3
        pitch = 10
        pad_x = 7
    cursor_y = y + 6
    while cursor_y + line_thickness <= y + height - 5:
        cursor_x = x + pad_x
        while cursor_x < x + width - pad_x:
            remaining = x + width - pad_x - cursor_x
            segment = min(remaining, rng.randint(3, 18))
            if segment <= 0:
                break
            shade = rng.randint(18, 84)
            canvas[cursor_y : cursor_y + line_thickness, cursor_x : cursor_x + segment] = shade
            cursor_x += segment + rng.randint(1, 5)
        cursor_y += pitch


def draw_image_block(
    canvas: np.ndarray,
    bbox: tuple[int, int, int, int],
    rng: random.Random,
) -> None:
    x, y, width, height = bbox
    draw_border(canvas, bbox, value=24)
    for yy in range(y + 2, y + height - 2):
        for xx in range(x + 2, x + width - 2):
            if rng.random() < 0.62:
                canvas[yy, xx] = rng.randint(12, 118)


def add_noise(canvas: np.ndarray, page_number: int, rng: random.Random) -> None:
    for _ in range(2):
        x = rng.randint(8, 24)
        y = rng.randint(120, PAGE_HEIGHT - 80)
        size = rng.choice([3, 4])
        canvas[y : y + size, x : x + size] = 0


def make_block(
    *,
    block_id: str,
    bbox: tuple[int, int, int, int],
    role: str,
    kind: str,
    column_group: str,
    row_group: int,
) -> dict[str, Any]:
    return {
        "id": block_id,
        "bbox": list(bbox),
        "role": role,
        "kind": kind,
        "column_group": column_group,
        "row_group": row_group,
    }


def layout_blocks(layout: str, page_number: int) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    multi_column = layout in {"double", "triple", "mixed"}
    span_group = "span" if multi_column else "c0"
    serial = 0

    def add(
        bbox: tuple[int, int, int, int],
        role: str,
        kind: str,
        column_group: str,
        row_group: int,
    ) -> dict[str, Any]:
        nonlocal serial
        serial += 1
        block = make_block(
            block_id=f"t{page_number:04d}_{serial:03d}",
            bbox=bbox,
            role=role,
            kind=kind,
            column_group=column_group,
            row_group=row_group,
        )
        blocks.append(block)
        return block

    add((40, 20, 640, 30), "header_footer", "header_footer", span_group, 0)
    add((40, 68, 640, 108), "title", "title", span_group, 1)

    if layout == "single":
        add((40, 206, 640, 88), "body", "body", "c0", 2)
        add((40, 316, 640, 88), "body", "body", "c0", 3)
        add((40, 426, 640, 88), "body", "body", "c0", 4)
        add((40, 536, 640, 170), "image", "image", "c0", 5)
        add((40, 726, 640, 88), "body", "body", "c0", 6)
        footer_row = 7
    elif layout == "double":
        columns = [40, 380]
        for row_index, y in enumerate((210, 316, 422, 528, 634), start=2):
            for column_index, x in enumerate(columns):
                add((x, y, 300, 88), "body", "body", f"c{column_index}", row_index)
        footer_row = 7
    elif layout == "triple":
        columns = [40, 265, 490]
        for row_index, y in enumerate((210, 316, 422, 528, 634), start=2):
            for column_index, x in enumerate(columns):
                add((x, y, 190, 88), "body", "body", f"c{column_index}", row_index)
        footer_row = 7
    elif layout == "mixed":
        for row_index, y in enumerate((210, 316, 422, 528, 634), start=2):
            add((40, y, 300, 88), "body", "body", "c0", row_index)
        add((380, 210, 300, 180), "image", "image", "c1", 2)
        add((380, 410, 300, 88), "body", "body", "c1", 4)
        add((380, 516, 300, 88), "body", "body", "c1", 5)
        add((380, 622, 300, 88), "body", "body", "c1", 6)
        add((40, 750, 640, 120), "image", "image", span_group, 7)
        footer_row = 8
    else:
        raise ValueError(f"unknown layout {layout}")

    add((40, 908, 640, 30), "header_footer", "header_footer", span_group, footer_row)
    return blocks


def _column_first_order(blocks: list[dict[str, Any]]) -> list[str]:
    groups: dict[str, list[dict[str, Any]]] = {}
    spanning: list[dict[str, Any]] = []
    for block in blocks:
        if block["column_group"] == "span":
            spanning.append(block)
        else:
            groups.setdefault(block["column_group"], []).append(block)
    spanning.sort(key=lambda block: (block["bbox"][1], block["bbox"][0], block["id"]))
    if len(groups) <= 1 and not spanning:
        return [
            block["id"]
            for block in sorted(
                blocks,
                key=lambda item: (item["bbox"][1], item["bbox"][0], item["id"]),
            )
        ]
    regular: list[dict[str, Any]] = []
    for group_id in sorted(groups):
        regular.extend(
            sorted(
                groups[group_id],
                key=lambda item: (item["bbox"][1], item["bbox"][0], item["id"]),
            )
        )
    regular.sort(
        key=lambda item: (
            int(item["column_group"][1:]),
            item["bbox"][1],
            item["bbox"][0],
            item["id"],
        )
    )
    by_band: dict[int, list[dict[str, Any]]] = {index: [] for index in range(len(spanning) + 1)}
    for block in regular:
        band = sum(1 for item in spanning if item["bbox"][1] <= block["bbox"][1])
        by_band[band].append(block)
    sequence: list[str] = []
    for band in range(len(spanning) + 1):
        sequence.extend(block["id"] for block in by_band[band])
        if band < len(spanning):
            sequence.append(spanning[band]["id"])
    return sequence


def _region_first_order(blocks: list[dict[str, Any]], tolerance: int = 24) -> list[str]:
    spanning = sorted(
        [block for block in blocks if block["column_group"] == "span"],
        key=lambda block: (block["bbox"][1], block["bbox"][0], block["id"]),
    )
    regular = [block for block in blocks if block["column_group"] != "span"]
    by_band: dict[int, list[dict[str, Any]]] = {index: [] for index in range(len(spanning) + 1)}
    for block in regular:
        band = sum(1 for item in spanning if item["bbox"][1] <= block["bbox"][1])
        by_band[band].append(block)
    sequence: list[str] = []
    for boundary_band in range(len(spanning) + 1):
        ordered = sorted(
            by_band[boundary_band],
            key=lambda item: (item["bbox"][1], item["bbox"][0], item["id"]),
        )
        region_bands: dict[str, int] = {}
        current_band = -1
        band_start = 0
        for block in ordered:
            y = block["bbox"][1]
            if current_band < 0 or y - band_start > tolerance:
                current_band += 1
                band_start = y
            region_bands[block["id"]] = current_band
        ordered.sort(
            key=lambda item: (
                region_bands[item["id"]],
                item["bbox"][0],
                item["bbox"][1],
                item["id"],
            )
        )
        sequence.extend(block["id"] for block in ordered)
        if boundary_band < len(spanning):
            sequence.append(spanning[boundary_band]["id"])
    return sequence


def generate_page(layout: str, page_number: int, seed: int) -> tuple[np.ndarray, list[dict[str, Any]]]:
    blocks = layout_blocks(layout, page_number)
    canvas = np.full((PAGE_HEIGHT, PAGE_WIDTH), BACKGROUND, dtype=np.uint8)
    for index, block in enumerate(blocks):
        rng = random.Random(seed * 1_000_003 + page_number * 10_007 + index * 97)
        bbox = tuple(int(value) for value in block["bbox"])
        if block["role"] == "image":
            draw_image_block(canvas, bbox, rng)
        else:
            draw_text_block(canvas, bbox, rng, kind=block["kind"])
    add_noise(canvas, page_number, random.Random(seed * 17 + page_number))
    return canvas, blocks


def page_truth(page_name: str, layout: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    column_order = _column_first_order(blocks)
    region_order = _region_first_order(blocks)
    column_index = {block_id: index + 1 for index, block_id in enumerate(column_order)}
    region_index = {block_id: index + 1 for index, block_id in enumerate(region_order)}
    truth_blocks = []
    for block in blocks:
        truth_blocks.append(
            {
                "id": block["id"],
                "bbox": block["bbox"],
                "role": block["role"],
                "column_group": block["column_group"],
                "row_group": block["row_group"],
                "reading_index": column_index[block["id"]],
                "reading_index_by_rule": {
                    "column_first": column_index[block["id"]],
                    "region_first": region_index[block["id"]],
                },
            }
        )
    groups: dict[str, list[str]] = {}
    for block in blocks:
        groups.setdefault(block["column_group"], []).append(block["id"])
    return {
        "page": page_name,
        "layout": layout,
        "blocks": truth_blocks,
        "column_groups": groups,
        "reading_orders": {
            "column_first": column_order,
            "region_first": region_order,
        },
    }


def sample_config(seed: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "compatibility_tag": "uid103-synthetic-layout-v1",
        "analysis": {
            "binary_threshold": 128,
            "min_block_width": 10,
            "min_block_height": 10,
            "projection_analysis_window": 3,
            "title_height_ratio_range": [1.15, 1.60],
            "body_height_ratio_range": [0.75, 1.14],
            "image_ink_density_threshold": 0.42,
            "text_ink_density_range": [0.05, 0.38],
            "projection_periodicity_threshold": 0.20,
            "role_priority": ["header_footer", "image", "title", "body"],
            "header_band_ratio": 0.10,
            "footer_band_ratio": 0.10,
        },
        "columns": {
            "enabled": True,
            "gap_threshold": 24,
            "overlap_threshold": 0.55,
            "spanning_width_ratio": 0.72,
        },
        "reading": {
            "rule": "column_first",
            "region_band_tolerance": 24,
        },
        "runtime": {
            "thread_pool_workers": 4,
            "batch_page_budget": 64,
            "interaction_latency_budget_ms": 500,
            "snapshot_commit_interval": 4,
        },
        "adaptive": {
            "enabled": True,
            "balanced_pressure": 1.2,
            "fast_pressure": 8.0,
        },
        "generator_seed": seed,
    }


def generate(out_dir: Path, pages: int, seed: int) -> dict[str, Any]:
    layouts = ["single", "double", "triple", "mixed"]
    images_dir = out_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    truth_pages: list[dict[str, Any]] = []
    group_labels: dict[str, str] = {}
    total_blocks = 0
    group_counts = {"single": 0, "double": 0, "triple": 0, "mixed": 0}
    group_totals = {"single": 0, "double": 0, "triple": 0, "mixed": 0}
    for page_number in range(1, pages + 1):
        layout = layouts[(page_number - 1) % len(layouts)]
        page_name = f"page_{page_number:04d}.png"
        canvas, blocks = generate_page(layout, page_number, seed)
        write_gray_png(images_dir / page_name, canvas)
        truth_pages.append(page_truth(page_name, layout, blocks))
        group_labels[page_name] = layout
        group_counts[layout] += 1
        group_totals[layout] += len(blocks)
        total_blocks += len(blocks)

    average_columns = statistics_weighted_average(group_counts)
    metadata = {
        "schema_version": 1,
        "seed": seed,
        "page_width": PAGE_WIDTH,
        "page_height": PAGE_HEIGHT,
        "pixel_mode": "L",
        "sample_groups": layouts,
        "group_labels": group_labels,
        "expected_reading_order_entries": total_blocks,
        "total_pages": pages,
        "layout_profile": {
            "average_blocks_per_page": round(total_blocks / max(1, pages), 6),
            "average_column_groups": round(average_columns, 6),
            "average_blocks_by_group": {
                layout: round(group_totals[layout] / max(1, group_counts[layout]), 6)
                for layout in layouts
            },
        },
    }
    truth = {
        "schema_version": 1,
        "seed": seed,
        "page_width": PAGE_WIDTH,
        "page_height": PAGE_HEIGHT,
        "expected_reading_order_entries": total_blocks,
        "pages": truth_pages,
    }
    config = sample_config(seed)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "layout_truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    replacements_dir = out_dir / "replacements"
    for name, layout in (("page_alt_double.png", "double"), ("page_alt_mixed.png", "mixed")):
        canvas, _ = generate_page(layout, 9000 + layouts.index(layout), seed + 17)
        write_gray_png(replacements_dir / name, canvas)
    return {
        "pages": pages,
        "blocks": total_blocks,
        "out_dir": str(out_dir),
        "config": config,
        "metadata": metadata,
        "truth": truth,
    }


def statistics_weighted_average(group_counts: dict[str, int]) -> float:
    weights = {"single": 1.0, "double": 2.0, "triple": 3.0, "mixed": 2.0}
    total = sum(group_counts.values())
    if total == 0:
        return 1.0
    return sum(weights[name] * count for name, count in group_counts.items()) / total


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic synthetic layout samples")
    parser.add_argument("--out", type=Path, default=Path("samples"))
    parser.add_argument("--pages", type=int, default=16)
    parser.add_argument("--seed", type=int, default=20261117)
    args = parser.parse_args()
    if args.pages < 1:
        parser.error("--pages must be positive")
    result = generate(args.out, args.pages, args.seed)
    print(
        json.dumps(
            {
                "out_dir": result["out_dir"],
                "pages": result["pages"],
                "blocks": result["blocks"],
                "seed": args.seed,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
