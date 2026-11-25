#!/usr/bin/env python3
"""Deterministic offline samples for the generative-image diagnostics bench.

The generator only uses the fixed seed and NumPy's stable legacy RNG.  It emits
JSONL edit tasks plus a parameter JSON containing the controlled vocabulary,
colour matrix, budgets and precision settings.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import numpy as np


VOCAB = [
    "watercolor", "oil", "cyberpunk", "soft_light", "sharp",
    "warm_tone", "cool_tone", "studio", "bokeh", "texture",
    "cinematic", "pastel", "ink", "neon", "matte",
]

SHAPES = [(32, 40, 3), (40, 48, 3), (48, 64, 3), (36, 56, 3)]
BLOCK_SIZES = [8, 12, 16, 20]
PROFILES = ["float64", "float32", "fixed"]
ROUNDING = ["nearest", "floor", "ceil"]


def _polygon(rng: np.random.RandomState, height: int, width: int) -> list[list[int]]:
    """Create a simple, in-bounds convex-ish polygon."""
    margin_x = max(5, width // 8)
    margin_y = max(5, height // 8)
    cx = int(rng.randint(margin_x + 4, width - margin_x - 4))
    cy = int(rng.randint(margin_y + 4, height - margin_y - 4))
    rx = int(rng.randint(max(4, width // 8), max(6, width // 4)))
    ry = int(rng.randint(max(4, height // 8), max(6, height // 4)))
    count = int(rng.randint(5, 8))
    points = []
    for index in range(count):
        angle = 2.0 * np.pi * index / count + rng.uniform(-0.16, 0.16)
        radius_x = rx * rng.uniform(0.72, 1.0)
        radius_y = ry * rng.uniform(0.72, 1.0)
        x = int(round(cx + np.cos(angle) * radius_x))
        y = int(round(cy + np.sin(angle) * radius_y))
        x = min(width - 2, max(1, x))
        y = min(height - 2, max(1, y))
        points.append([x, y])
    # A convex hull is unnecessary: angle-ordered perturbed points are simple.
    return points


def build_task(case_index: int, seed: int) -> dict[str, Any]:
    rng = np.random.RandomState(seed * 1_000_003 + case_index + 17)
    height, width, channels = SHAPES[case_index % len(SHAPES)]
    profile = PROFILES[(case_index + (case_index // 7)) % len(PROFILES)]
    words = [VOCAB[int(rng.randint(0, len(VOCAB)))] for _ in range(int(rng.randint(3, 7)))]
    # Guarantee at least one stable style word while retaining word-frequency
    # weighting through duplicated draws.
    words.append(VOCAB[case_index % 5])
    task: dict[str, Any] = {
        "edit_id": f"edit-{case_index:04d}",
        "image_seed": int(seed * 7919 + case_index * 104729 + 1),
        "image_shape": [height, width, channels],
        "mask": {"polygons": [_polygon(rng, height, width)]},
        "prompt": " ".join(words),
        "style_strength": round(float(rng.uniform(0.15, 0.95)), 4),
        "iterations": int(rng.randint(1, 5)),
        "block_size": int(BLOCK_SIZES[case_index % len(BLOCK_SIZES)]),
        "dtype_profile": profile,
        "quant_scale": int([64, 128, 256][case_index % 3]),
        "rounding_mode": ROUNDING[case_index % len(ROUNDING)],
        "requested_block_concurrency": int(1 + case_index % 3),
        "revision": 1,
    }
    return task


def build_invalid_cases() -> list[dict[str, Any]]:
    base = build_task(900, 0)
    cases = []

    degenerate = json.loads(json.dumps(base))
    degenerate["edit_id"] = "invalid-mask-degenerate"
    poly = degenerate["mask"]["polygons"][0]
    poly.insert(2, list(poly[1]))
    degenerate["revision"] = 1
    cases.append(degenerate)

    intersect = json.loads(json.dumps(base))
    intersect["edit_id"] = "invalid-mask-intersect"
    h, w, _ = intersect["image_shape"]
    intersect["mask"] = {"polygons": [[
        [w // 4, 4], [3 * w // 4, h - 5], [w // 4, h - 5], [3 * w // 4, 4]
    ]]}
    cases.append(intersect)

    oob = json.loads(json.dumps(base))
    oob["edit_id"] = "invalid-mask-oob"
    oob["mask"] = {"polygons": [[[2, 2], [w + 25, 3], [4, h - 4]]]}
    cases.append(oob)

    style = json.loads(json.dumps(base))
    style["edit_id"] = "invalid-style-strength"
    style["style_strength"] = 1.75
    cases.append(style)

    matrix = json.loads(json.dumps(base))
    matrix["edit_id"] = "invalid-transform-matrix"
    matrix["color_matrix"] = [[1.0, 0.0], [0.0, 1.0]]
    cases.append(matrix)

    memory = json.loads(json.dumps(base))
    memory["edit_id"] = "invalid-memory-budget"
    memory["image_shape"] = [4096, 4096, 3]
    memory["mask"] = {"polygons": [[[10, 10], [4000, 12], [2000, 4000]]]}
    memory["block_size"] = 64
    memory["requested_block_concurrency"] = 8
    cases.append(memory)

    concurrency = json.loads(json.dumps(base))
    concurrency["edit_id"] = "invalid-block-concurrency"
    concurrency["requested_block_concurrency"] = 99
    cases.append(concurrency)
    return cases


def build_params(seed: int) -> dict[str, Any]:
    return {
        "seed": seed,
        "text_vocab": VOCAB,
        "text_dim": 24,
        "color_matrix": [
            [1.08, 0.03, -0.02],
            [0.02, 0.96, 0.04],
            [-0.03, 0.02, 1.05],
        ],
        "fusion_feather": 2.0,
        "boundary_neighbors": 8,
        "numeric_epsilon": 1e-12,
        "budget": {
            "worker_count": 4,
            "batch_size": 10,
            "max_total_resident_bytes": 12 * 1024 * 1024,
            "max_block_concurrency": 4,
            "single_block_max_bytes": 256 * 1024,
            "pixel_buffer_count": 5,
            "context_bytes_per_sample": 96,
            "block_overhead_bytes": 4096,
        },
        "policy": {
            "priority": ["quality", "memory", "progress"],
            "allow_smaller_block": True,
            "allow_serial": True,
            "minimum_block_size": 4,
            "validation_strength": "strict",
        },
        "quantization": {
            "default_quant_scale": 128,
            "rounding_mode": "nearest",
        },
        "snapshot_version": "snapshot-v1",
    }


def write_jsonl(path: str, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--case_range", type=int, nargs=2, metavar=("START", "END"),
                        default=(0, 50))
    parser.add_argument("--out", default="samples")
    args = parser.parse_args()

    start, end = args.case_range
    if end < start:
        raise SystemExit("--case_range END must be >= START")
    os.makedirs(args.out, exist_ok=True)
    tasks = [build_task(index, args.seed) for index in range(start, end)]
    params = build_params(args.seed)

    task_path = os.path.join(args.out, "edit_tasks.jsonl")
    params_path = os.path.join(args.out, "params.json")
    invalid_path = os.path.join(args.out, "invalid_cases.jsonl")
    write_jsonl(task_path, tasks)
    with open(params_path, "w", encoding="utf-8") as handle:
        json.dump(params, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    write_jsonl(invalid_path, build_invalid_cases())

    # A deterministic revision fixture: only edit-0003's style_strength and
    # revision change; masks and all other tasks stay byte-identical.
    revised = [json.loads(json.dumps(task)) for task in tasks]
    if len(revised) >= 4:
        revised[3]["revision"] = 2
        revised[3]["style_strength"] = round(
            min(1.0, float(revised[3]["style_strength"]) + 0.17), 4
        )
    revision_path = os.path.join(args.out, "edit_tasks_rev2_style.jsonl")
    write_jsonl(revision_path, revised)

    mask_revised = [json.loads(json.dumps(task)) for task in tasks]
    if len(mask_revised) >= 6:
        mask_revised[5]["revision"] = 2
        h, w, _ = mask_revised[5]["image_shape"]
        mask_revised[5]["mask"] = {"polygons": [[
            [max(2, w // 5), max(2, h // 5)],
            [min(w - 2, 2 * w // 3), max(3, h // 4)],
            [min(w - 2, 3 * w // 4), min(h - 3, 3 * h // 4)],
            [max(3, w // 4), min(h - 3, 4 * h // 5)],
        ]]}
    mask_revision_path = os.path.join(args.out, "edit_tasks_rev2_mask.jsonl")
    write_jsonl(mask_revision_path, mask_revised)

    print(f"wrote {len(tasks)} tasks -> {task_path}")
    print(f"wrote parameters -> {params_path}")
    print(f"wrote invalid cases -> {invalid_path}")
    print(f"wrote revision fixtures -> {revision_path}, {mask_revision_path}")


if __name__ == "__main__":
    main()
