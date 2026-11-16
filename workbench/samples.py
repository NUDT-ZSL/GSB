"""Deterministic local sample generation (fixed seed, no network, no GPU).

Generates into a data directory:
  images/*.png        fixed-size PNGs with geometric primitives, color blocks, noise
  relations.json      region relation table (contains / adjacent / occludes)
  config.json         description generation configuration
  exchange_v1.txt     v1 fixed-width plain-text description exchange file
  exchange_v2.json    v2 JSON description exchange file
  injection.json      standalone fault-injection configuration
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

SEED = 20261117
WIDTH = 256
HEIGHT = 256

# inst_id, category, color_name, rgb, bbox (x0, y0, x1, y1), conf_permille
SHAPES = [
    ("INST0001", "rectangle", "red",     (220, 40, 40),   (20, 20, 90, 80),    920),
    ("INST0002", "circle",    "green",   (40, 200, 60),   (130, 30, 190, 90),  880),
    ("INST0003", "triangle",  "blue",    (50, 80, 220),   (30, 120, 90, 180),  810),
    ("INST0004", "rectangle", "yellow",  (230, 220, 60),  (120, 120, 210, 200), 940),
    ("INST0005", "circle",    "magenta", (220, 60, 200),  (150, 140, 180, 170), 870),
    ("INST0006", "rectangle", "cyan",    (60, 210, 210),  (196, 150, 236, 196), 760),
    ("INST0007", "circle",    "orange",  (240, 160, 40),  (60, 190, 110, 240), 500),
]

# v2-only precision: half-pixel bbox for INST0006 (rounding residual exactly 0.5)
V2_BBOX_OVERRIDES = {
    "INST0006": [196.5, 150.5, 236.5, 196.5],
}

RELATIONS = [
    {"id": "R1", "type": "contains", "a": "INST0004", "b": "INST0005"},
    {"id": "R2", "type": "adjacent", "a": "INST0001", "b": "INST0002"},
    {"id": "R3", "type": "occludes", "a": "INST0003", "b": "INST0001"},
]

RELATION_REFS = {
    "INST0001": ["R2", "R3"],
    "INST0002": ["R2"],
    "INST0003": ["R3"],
    "INST0004": ["R1"],
    "INST0005": ["R1"],
    "INST0006": [],
    "INST0007": [],
}

CONFIG = {
    "version": 1,
    "seed": SEED,
    "image": {"width": WIDTH, "height": HEIGHT},
    "quantization_levels": 16,
    "min_region_area": 40,
    "confidence_threshold": 0.5,
    "boundary_epsilon": 0.02,
    "template_weights": {"compact": 0.6, "verbose": 0.4},
    "target_format_version": 2,
    "concurrency": 4,
    "tolerance": {"compactness": 0.5, "area_norm": 0.2},
    "categories": {
        "circle":    {"weights": {"compactness": 0.8, "area_norm": 0.2},
                      "targets": {"compactness": 0.785, "area_norm": 0.05}},
        "rectangle": {"weights": {"compactness": 0.8, "area_norm": 0.2},
                      "targets": {"compactness": 1.0, "area_norm": 0.08}},
        "triangle":  {"weights": {"compactness": 0.8, "area_norm": 0.2},
                      "targets": {"compactness": 0.5, "area_norm": 0.04}},
    },
}

INJECTIONS = [
    {"id": "INJ-001", "type": "missing_field_line", "target": "v1", "line": 1},
    {"id": "INJ-002", "type": "invalid_json", "target": "v2"},
    {"id": "INJ-003", "type": "bbox_out_of_bounds", "target": "v2", "inst": "INST0002"},
    {"id": "INJ-004", "type": "confidence_out_of_range", "target": "v2", "inst": "INST0003"},
    {"id": "INJ-005", "type": "dangling_relation", "target": "v2", "inst": "INST0004"},
]


def _draw_shape(draw: ImageDraw.ImageDraw, category: str, bbox, rgb) -> None:
    x0, y0, x1, y1 = bbox
    if category == "rectangle":
        draw.rectangle([x0, y0, x1 - 1, y1 - 1], fill=rgb)
    elif category == "circle":
        draw.ellipse([x0, y0, x1 - 1, y1 - 1], fill=rgb)
    elif category == "triangle":
        draw.polygon([(x0, y1 - 1), ((x0 + x1) // 2, y0), (x1 - 1, y1 - 1)], fill=rgb)
    else:  # pragma: no cover - defensive
        raise ValueError(category)


def generate_image(seed: int, shapes) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = 200 + rng.integers(0, 25, (HEIGHT, WIDTH, 3), dtype=np.int64)
    arr = np.clip(base, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr, "RGB")
    draw = ImageDraw.Draw(img)
    for _inst, category, _name, rgb, bbox, _conf in shapes:
        _draw_shape(draw, category, bbox, rgb)
    return np.asarray(img)


def build_config() -> dict:
    return json.loads(json.dumps(CONFIG))


def build_v1_text() -> str:
    from .v1io import format_v1_record

    lines = []
    for inst, category, _name, _rgb, bbox, conf in SHAPES:
        lines.append(format_v1_record({
            "inst_id": inst, "category": category,
            "bbox": list(bbox), "conf_permille": conf,
        }))
    return "\n".join(lines) + "\n"


def build_v2_doc() -> list:
    doc = [{
        "kind": "color_table",
        "colors": {name: list(rgb) for _i, _c, name, rgb, _b, _cf in SHAPES},
    }]
    for inst, category, name, _rgb, bbox, conf in SHAPES:
        bbox_f = V2_BBOX_OVERRIDES.get(inst, [float(v) for v in bbox])
        doc.append({
            "kind": "instance",
            "id": inst,
            "category": category,
            "bbox": bbox_f,
            "confidence": conf / 1000.0,
            "relations": list(RELATION_REFS[inst]),
            "color_name": name,
        })
    return doc


def generate_samples(out_dir) -> dict:
    """Generate all deterministic samples. Returns a manifest of written files."""
    out = Path(out_dir)
    img_dir = out / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    written = {}

    # Three deterministic images: same shape set shifted per image index.
    for img_idx in range(3):
        shifted = []
        dx, dy = img_idx * 3, img_idx * 2
        for inst, cat, name, rgb, bbox, conf in SHAPES:
            x0, y0, x1, y1 = bbox
            nb = (x0 + dx, y0 + dy, x1 + dx, y1 + dy)
            shifted.append((inst, cat, name, rgb, nb, conf))
        arr = generate_image(SEED + img_idx, shifted)
        path = img_dir / f"sample_{img_idx:02d}.png"
        Image.fromarray(arr, "RGB").save(path, format="PNG")
        written[f"images/{path.name}"] = path.stat().st_size

    files = {
        "relations.json": json.dumps(RELATIONS, indent=2, ensure_ascii=False),
        "config.json": json.dumps(build_config(), indent=2, ensure_ascii=False),
        "exchange_v1.txt": build_v1_text(),
        "exchange_v2.json": json.dumps(build_v2_doc(), indent=2, ensure_ascii=False),
        "injection.json": json.dumps(INJECTIONS, indent=2, ensure_ascii=False),
    }
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
        written[name] = len(text)
    return written
