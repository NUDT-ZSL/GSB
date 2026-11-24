from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .hashing import sha256_file
from .imaging import bbox_of_mask, rotate_grayscale, write_pgm
from .glyphs import render_word_mask


WIDTH = 960
HEIGHT = 640
SEED = 20261125


@dataclass(frozen=True)
class FormLayout:
    image_id: str
    angle: float
    blocks: list[dict[str, Any]]


def _layouts() -> list[FormLayout]:
    common_rows = [
        ("NAME", 92, 148, True),
        ("ALICE", 345, 148, False),
        ("TEAM", 92, 243, True),
        ("RED", 345, 243, False),
        ("LEVEL", 92, 338, True),
        ("MAGE", 345, 338, False),
        ("SERVER", 92, 433, True),
        ("ASIA", 345, 433, False),
        ("CODE", 92, 528, True),
        ("PLAYER01", 345, 528, False),
    ]
    variants = [
        ("form-001", -0.35, "GAME EVENT", common_rows),
        (
            "form-002",
            0.30,
            "JOY DAY",
            [
                ("NAME", 92, 148, True),
                ("BOB", 345, 148, False),
                ("TEAM", 92, 243, True),
                ("BLUE", 345, 243, False),
                ("LEVEL", 92, 338, True),
                ("WARRIOR", 345, 338, False),
                ("SERVER", 92, 433, True),
                ("EU", 345, 433, False),
                ("CODE", 92, 528, True),
                ("X7Y", 345, 528, False),
            ],
        ),
        (
            "form-003",
            -0.18,
            "LUCK DRAW",
            [
                ("NAME", 92, 148, True),
                ("CAROL", 345, 148, False),
                ("TEAM", 92, 243, True),
                ("GREEN", 345, 243, False),
                ("LEVEL", 92, 338, True),
                ("ARCHER", 345, 338, False),
                ("SERVER", 92, 433, True),
                ("NA", 345, 433, False),
                ("CODE", 92, 528, True),
                ("8848", 345, 528, False),
            ],
        ),
    ]
    layouts: list[FormLayout] = []
    for image_id, angle, title, rows in variants:
        blocks = [{"block_id": f"{image_id}-title", "text": title, "x": 300, "y": 38, "printed": True}]
        for index, (text, x, y, printed) in enumerate(rows):
            blocks.append(
                {
                    "block_id": f"{image_id}-b{index:02d}",
                    "text": text,
                    "x": x,
                    "y": y,
                    "printed": printed,
                }
            )
        layouts.append(FormLayout(image_id=image_id, angle=angle, blocks=blocks))

    for image_id in ("form-corrupt", "form-grid", "form-ledger", "form-vocab"):
        blocks = [{"block_id": f"{image_id}-title", "text": "GAME EVENT", "x": 300, "y": 38, "printed": True}]
        for index, (text, x, y, printed) in enumerate(common_rows):
            blocks.append(
                {
                    "block_id": f"{image_id}-b{index:02d}",
                    "text": text,
                    "x": x,
                    "y": y,
                    "printed": printed,
                }
            )
        layouts.append(FormLayout(image_id=image_id, angle=0.22, blocks=blocks))
    return layouts


def _draw_table(image: np.ndarray) -> None:
    from .imaging import draw_line

    line_value = 105
    for y in (120, 215, 310, 405, 500, 590):
        draw_line(image, 60, y, 900, y, value=line_value, width=3)
    for x in (60, 300, 620, 900):
        draw_line(image, x, 120, x, 590, value=line_value, width=3)


def render_form(layout: FormLayout, seed: int) -> tuple[np.ndarray, list[dict[str, Any]]]:
    stable_image_seed = zlib.crc32(layout.image_id.encode("utf-8")) % 100000
    rng = np.random.RandomState(seed + stable_image_seed)
    image = np.full((HEIGHT, WIDTH), 245, dtype=np.uint8)
    noise = rng.randint(-5, 6, size=image.shape)
    image = np.clip(image.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    _draw_table(image)

    text_mask = np.zeros_like(image, dtype=np.uint8)
    truth: list[dict[str, Any]] = []
    for block in layout.blocks:
        block_rng = np.random.RandomState(seed + sum(map(ord, block["block_id"])))
        mask = render_word_mask(
            block["text"],
            cell=8,
            bold=bool(block["printed"]),
            jitter_rng=None if block["printed"] else block_rng,
            handwritten=not block["printed"],
        ).astype(np.uint8)
        h, w = mask.shape
        x, y = int(block["x"]), int(block["y"])
        region = image[y : y + h, x : x + w]
        ink = mask.astype(bool)
        gray = int(28 + block_rng.randint(0, 24))
        region[ink] = gray
        text_mask[y : y + h, x : x + w][ink] = 1

    image = rotate_grayscale(image, layout.angle, fill=245)
    rotated_mask = rotate_grayscale(text_mask, layout.angle, fill=0) > 0

    pepper_y = rng.randint(0, HEIGHT, size=650)
    pepper_x = rng.randint(0, WIDTH, size=650)
    image[pepper_y, pepper_x] = rng.randint(20, 90, size=650).astype(np.uint8)
    salt_y = rng.randint(0, HEIGHT, size=450)
    salt_x = rng.randint(0, WIDTH, size=450)
    image[salt_y, salt_x] = 245

    # Truth rectangles come from the transformed deterministic text mask. To keep
    # block identity, re-render each mask and transform its four corners.
    center_x = (WIDTH - 1) / 2.0
    center_y = (HEIGHT - 1) / 2.0
    theta = np.deg2rad(layout.angle)
    cosine = np.cos(theta)
    sine = np.sin(theta)
    for block in layout.blocks:
        block_rng = np.random.RandomState(seed + sum(map(ord, block["block_id"])))
        mask = render_word_mask(
            block["text"],
            cell=8,
            bold=bool(block["printed"]),
            jitter_rng=None if block["printed"] else block_rng,
            handwritten=not block["printed"],
        )
        h, w = mask.shape
        corners = [(block["x"], block["y"]), (block["x"] + w, block["y"]),
                   (block["x"], block["y"] + h), (block["x"] + w, block["y"] + h)]
        transformed: list[tuple[float, float]] = []
        for x, y in corners:
            dx = x - center_x
            dy = y - center_y
            transformed.append(
                (center_x + cosine * dx + sine * dy, center_y - sine * dx + cosine * dy)
            )
        x0 = max(0, int(np.floor(min(x for x, _ in transformed))))
        y0 = max(0, int(np.floor(min(y for _, y in transformed))))
        x1 = min(WIDTH, int(np.ceil(max(x for x, _ in transformed))))
        y1 = min(HEIGHT, int(np.ceil(max(y for _, y in transformed))))
        truth.append(
            {
                "block_id": block["block_id"],
                "bbox": [x0, y0, x1 - x0, y1 - y0],
                "text": " ".join(str(block["text"]).upper().split()),
                "confidence_lower_bound": 0.72,
            }
        )
    truth.sort(key=lambda item: (item["bbox"][0], item["bbox"][1], item["bbox"][2], item["bbox"][3]))
    return image, truth


def generate_dataset(root: str | Path) -> dict[str, Any]:
    root = Path(root).resolve()
    image_dir = root / "samples" / "images"
    truth_dir = root / "samples" / "truth"
    image_dir.mkdir(parents=True, exist_ok=True)
    truth_dir.mkdir(parents=True, exist_ok=True)

    layouts = _layouts()
    image_records: list[dict[str, Any]] = []
    vocabulary = {
        "GAME EVENT",
        "JOY DAY",
        "LUCK DRAW",
        "NAME",
        "ALICE",
        "BOB",
        "CAROL",
        "TEAM",
        "RED",
        "BLUE",
        "GREEN",
        "LEVEL",
        "MAGE",
        "WARRIOR",
        "ARCHER",
        "SERVER",
        "ASIA",
        "EU",
        "NA",
        "CODE",
        "PLAYER01",
        "X7Y",
        "8848",
    }

    for layout in layouts:
        image, truth = render_form(layout, SEED)
        image_path = image_dir / f"{layout.image_id}.pgm"
        truth_path = truth_dir / f"{layout.image_id}.json"
        write_pgm(image_path, image)
        truth_path.write_text(
            json.dumps(
                {
                    "image_id": layout.image_id,
                    "seed": SEED,
                    "blocks": truth,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        vocabulary.update(block["text"] for block in truth)
        image_records.append(
            {
                "image_id": layout.image_id,
                "path": f"images/{layout.image_id}.pgm",
                "truth_path": f"truth/{layout.image_id}.json",
                "sha256": sha256_file(image_path),
            }
        )

    # Inject corrupt pixels after the manifest digest was computed.
    corrupt_path = image_dir / "form-corrupt.pgm"
    from .imaging import read_pgm

    corrupt = read_pgm(corrupt_path)
    rng = np.random.RandomState(SEED + 999)
    corrupt[250:330, 120:260] = rng.randint(0, 255, size=(80, 140), dtype=np.uint8)
    write_pgm(corrupt_path, corrupt)

    injections = [
        {"type": "corrupt_pixels", "image_id": "form-corrupt", "field": "pixels[250:330,120:260]"},
        {"type": "grid_not_divisible", "image_id": "form-grid", "grid": {"rows": 3, "cols": 7}},
        {
            "type": "ledger_digest_mismatch",
            "image_id": "form-ledger",
            "block_index": 2,
        },
        {
            "type": "vocabulary_missing",
            "image_id": "form-vocab",
            "word": "MAGE",
            "block_index": 5,
        },
    ]

    task = {
        "schema_version": "1.0",
        "task_id": "game-offline-signup-deterministic",
        "seed": SEED,
        "image_width": WIDTH,
        "image_height": HEIGHT,
        "detection_threshold": 0.16,
        "confidence_floor": 0.70,
        "chunk_grid": {"rows": 4, "cols": 4},
        "concurrency": 4,
        "memory_budget_bytes": 8 * 1024 * 1024,
        "round_logic_budget": 42.0,
        "state_dir": "state",
        "output_dir": "output",
        "normalization": {
            "target_cap_height": 40,
            "template_cell": 8,
            "ink_cutoff": 175,
        },
        "vocabulary": sorted(vocabulary),
        "images": image_records,
        "exception_injections": injections,
    }
    task_path = root / "samples" / "task.json"
    task_path.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return task
