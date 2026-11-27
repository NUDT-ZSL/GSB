from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


REFERENCE_WIDTH = 1600
REFERENCE_HEIGHT = 2200
DEFAULT_SEED = 424242


@dataclass
class LayoutBlock:
    uid: str
    block_type: str
    bbox: tuple[int, int, int, int]
    drawing_box: tuple[int, int, int, int] | None = None
    anchor_hint: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class PageSpec:
    page_id: str
    blocks: list[LayoutBlock]
    reading_order: list[str]
    expected_conflicts: list[dict[str, Any]]
    seed_offset: int = 0


def generate_dataset(
    out_dir: str | Path,
    seed: int = DEFAULT_SEED,
    page_count: int = 3,
) -> dict[str, Any]:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    truth_dir = out_path / "truth"
    truth_dir.mkdir(parents=True, exist_ok=True)

    specs = build_page_specs(seed, page_count)
    manifest_pages: list[dict[str, Any]] = []
    for page_number, spec in enumerate(specs, start=1):
        page_dir = out_path / spec.page_id
        page_dir.mkdir(parents=True, exist_ok=True)
        reference = render_page(spec)

        variant_paths = {
            "lossless_png": page_dir / "reference_lossless.png",
            "jpeg_medium": page_dir / "medium_q90.jpg",
            "webp_compact": page_dir / "compact_q82.webp",
        }
        reference.save(variant_paths["lossless_png"], format="PNG", optimize=True)
        _save_jpeg(reference, variant_paths["jpeg_medium"], (1200, 1650), 90)
        _save_webp(reference, variant_paths["webp_compact"], (960, 1320), 82)

        truth = {
            "schema": "gsb-layout-truth/v1",
            "page_id": spec.page_id,
            "seed": seed,
            "reference_size": [REFERENCE_WIDTH, REFERENCE_HEIGHT],
            "blocks": [
                {
                    "uid": block.uid,
                    "type": block.block_type,
                    "bbox": list(block.bbox),
                    "anchor_hint": block.anchor_hint,
                    "meta": block.meta,
                }
                for block in spec.blocks
            ],
            "reading_order": spec.reading_order,
            "expected_conflicts": spec.expected_conflicts,
        }
        truth_path = truth_dir / f"{spec.page_id}.json"
        truth_path.write_text(
            json.dumps(truth, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        manifest_pages.append(
            {
                "page_id": spec.page_id,
                "page_number": page_number,
                "reference_size": [REFERENCE_WIDTH, REFERENCE_HEIGHT],
                "truth": str(truth_path.relative_to(out_path)),
                "variants": {
                    name: {
                        "path": str(path.relative_to(out_path)),
                        "size": list(_image_size(path)),
                    }
                    for name, path in variant_paths.items()
                },
            }
        )

    manifest = {
        "schema": "gsb-layout-sample-manifest/v1",
        "seed": seed,
        "reference_size": [REFERENCE_WIDTH, REFERENCE_HEIGHT],
        "pages": manifest_pages,
    }
    (out_path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _save_jpeg(image: Image.Image, path: Path, size: tuple[int, int], quality: int) -> None:
    resized = image.resize(size, Image.Resampling.LANCZOS)
    resized.save(path, format="JPEG", quality=quality, optimize=True, subsampling=0)


def _save_webp(image: Image.Image, path: Path, size: tuple[int, int], quality: int) -> None:
    resized = image.resize(size, Image.Resampling.LANCZOS)
    resized.save(path, format="WEBP", quality=quality, method=6)


def _image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def build_page_specs(seed: int, page_count: int) -> list[PageSpec]:
    if page_count < 1:
        raise ValueError("page_count must be at least 1")
    base = [_page_one(seed), _page_two(seed), _page_three_conflict(seed)]
    if page_count <= len(base):
        return base[:page_count]
    pages = list(base)
    for number in range(len(base) + 1, page_count + 1):
        pages.append(_page_variant(seed, number))
    return pages


def _body(
    uid: str,
    x: int,
    y: int,
    width: int,
    height: int,
    anchor_hint: str | None = None,
) -> LayoutBlock:
    return LayoutBlock(
        uid=uid,
        block_type="body_column",
        bbox=(x + 7, y + 5, x + width - 7, y + height - 5),
        drawing_box=(x, y, x + width, y + height),
        anchor_hint=anchor_hint,
        meta={"nominal_lane_width": width},
    )


def _annotation(
    uid: str,
    x: int,
    y: int,
    width: int,
    height: int,
    anchor_hint: str | None = None,
) -> LayoutBlock:
    return LayoutBlock(
        uid=uid,
        block_type="inline_annotation",
        bbox=(x + 4, y + 4, x + width - 4, y + height - 4),
        drawing_box=(x, y, x + width, y + height),
        anchor_hint=anchor_hint,
        meta={"subcolumns": 2, "nominal_lane_width": width},
    )


def _header(uid: str, x: int, y: int, width: int, height: int) -> LayoutBlock:
    return LayoutBlock(
        uid=uid,
        block_type="header_note",
        bbox=(x + 4, y + 4, x + width - 4, y + height - 4),
        drawing_box=(x, y, x + width, y + height),
        meta={"subcolumns": 3},
    )


def _page_one(seed: int) -> PageSpec:
    blocks = [
        _header("hdr01", 994, 78, 420, 252),
        _body("c01", 1400, 510, 100, 1390),
        _body("c02", 1250, 510, 100, 530),
        _annotation("a02", 1272, 1120, 56, 360, "c02"),
        _body("c03", 1100, 510, 100, 1390),
        _body("c04", 950, 510, 100, 430),
        _annotation("a04", 972, 1010, 56, 360, "c04"),
        _body("c05", 800, 510, 100, 1390),
        _body("c06", 650, 510, 100, 1120),
        LayoutBlock(
            "ill01",
            "illustration",
            (252, 904, 540, 1440),
            drawing_box=(252, 904, 540, 1440),
            anchor_hint="c06",
        ),
        LayoutBlock(
            "seal01",
            "seal",
            (254, 1762, 462, 1968),
            drawing_box=(254, 1762, 462, 1968),
            anchor_hint="c06",
        ),
    ]
    order = ["hdr01", "c01", "c02", "a02", "c03", "c04", "a04", "c05", "c06", "ill01", "seal01"]
    return PageSpec("page_001", blocks, order, [], seed_offset=1)


def _page_two(seed: int) -> PageSpec:
    blocks = [
        _header("hdr01", 918, 72, 486, 264),
        _body("c01", 1410, 500, 100, 1380),
        _body("c02", 1260, 500, 100, 880),
        _annotation("a02", 1282, 1450, 56, 348, "c02"),
        _body("c03", 1110, 500, 100, 1380),
        _body("c04", 960, 500, 100, 560),
        _annotation("a04", 982, 1130, 56, 360, "c04"),
        _body("c05", 810, 500, 100, 1380),
        _body("c06", 660, 500, 100, 1380),
        LayoutBlock(
            "ill01",
            "illustration",
            (252, 560, 548, 1060),
            drawing_box=(252, 560, 548, 1060),
            anchor_hint="c06",
        ),
        LayoutBlock(
            "seal01",
            "seal",
            (280, 1690, 492, 1928),
            drawing_box=(280, 1690, 492, 1928),
            anchor_hint="c06",
        ),
    ]
    order = ["hdr01", "c01", "c02", "a02", "c03", "c04", "a04", "c05", "c06", "ill01", "seal01"]
    return PageSpec("page_002", blocks, order, [], seed_offset=2)


def _page_three_conflict(seed: int) -> PageSpec:
    blocks = [
        _header("hdr01", 1050, 74, 360, 246),
        _body("c01", 1410, 506, 100, 1368),
        _body("c02", 1260, 506, 100, 690),
        _annotation("a02", 1282, 1272, 56, 300, "c02"),
        _body("c03", 1110, 506, 100, 690),
        _body("c04", 960, 506, 100, 1368),
        _body("c05", 810, 506, 100, 690),
        LayoutBlock(
            "a_bridge",
            "inline_annotation",
            (1204, 1554, 1264, 1866),
            drawing_box=(1200, 1550, 1268, 1870),
            anchor_hint=None,
        ),
        _body("c06", 660, 506, 100, 1368),
        LayoutBlock(
            "ill01",
            "illustration",
            (252, 890, 542, 1415),
            drawing_box=(252, 890, 542, 1415),
            anchor_hint="c06",
        ),
        LayoutBlock(
            "seal01",
            "seal",
            (260, 1738, 472, 1950),
            drawing_box=(260, 1738, 472, 1950),
            anchor_hint="c06",
        ),
    ]
    order = ["hdr01", "c01", "c02", "a02", "c03", "c04", "c05", "c06", "ill01", "seal01"]
    conflict = {
        "kind": "ambiguous_annotation_anchor",
        "involved": ["a_bridge", "c02", "c03"],
        "reason": "annotation lies midway between two body columns with equal horizontal relation",
    }
    return PageSpec("page_003", blocks, order, [conflict], seed_offset=3)


def _page_variant(seed: int, number: int) -> PageSpec:
    rng = random.Random((seed << 16) ^ number)
    illustration_x = 250 + rng.randrange(0, 10)
    illustration_y = 820 + rng.randrange(0, 60)
    seal_y = 1730 + rng.randrange(0, 20)
    blocks = [
        _header("hdr01", 900 + rng.randrange(0, 140), 70 + rng.randrange(0, 20), 420, 250),
        _body("c01", 1410, 500, 100, 1370),
        _body("c02", 1260, 500, 100, 690),
        _annotation("a02", 1282, 1290, 56, 340, "c02"),
        _body("c03", 1110, 500, 100, 1370),
        _body("c04", 960, 500, 100, 860),
        _annotation("a04", 982, 1470, 56, 340, "c04"),
        _body("c05", 810, 500, 100, 1370),
        _body("c06", 660, 500, 100, 1370),
        LayoutBlock(
            "ill01",
            "illustration",
            (illustration_x, illustration_y, illustration_x + 290, illustration_y + 500),
            drawing_box=(
                illustration_x,
                illustration_y,
                illustration_x + 290,
                illustration_y + 500,
            ),
            anchor_hint="c06",
        ),
        LayoutBlock(
            "seal01",
            "seal",
            (260, seal_y, 470, seal_y + 210),
            drawing_box=(260, seal_y, 470, seal_y + 210),
            anchor_hint="c06",
        ),
    ]
    order = ["hdr01", "c01", "c02", "a02", "c03", "c04", "a04", "c05", "c06", "ill01", "seal01"]
    return PageSpec(f"page_{number:03d}", blocks, order, [], seed_offset=number)


def render_page(spec: PageSpec) -> Image.Image:
    page_seed = int(hashlib.sha256(spec.page_id.encode("utf-8")).hexdigest()[:8], 16)
    rng = np.random.default_rng(page_seed)
    image = _paper_background(rng)
    draw = ImageDraw.Draw(image)
    ink = (39, 31, 26)
    faded = (72, 58, 44)

    for block in spec.blocks:
        box = block.drawing_box or block.bbox
        if block.block_type == "header_note":
            _draw_header(draw, box, ink, faded, page_seed)
        elif block.block_type == "body_column":
            _draw_body(draw, box, ink, page_seed)
        elif block.block_type == "inline_annotation":
            _draw_annotation(draw, box, ink, page_seed)
        elif block.block_type == "illustration":
            _draw_illustration(draw, box, page_seed)
        elif block.block_type == "seal":
            _draw_seal(draw, box, page_seed)
        else:
            raise ValueError(f"unknown block type: {block.block_type}")

    # A very light local blur imitates scanner optics without erasing hairlines.
    image = image.filter(ImageFilter.GaussianBlur(radius=0.45))
    arr = np.asarray(image, dtype=np.int16)
    grain = rng.normal(0.0, 2.2, size=(arr.shape[0], arr.shape[1], 1))
    arr = np.clip(arr + grain, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, mode="RGB")


def _paper_background(rng: np.random.Generator) -> Image.Image:
    height, width = REFERENCE_HEIGHT, REFERENCE_WIDTH
    yy, xx = np.mgrid[0:height, 0:width]
    base = (
        239
        - 7 * np.sin(xx / 180.0) * np.cos(yy / 230.0)
        - 4 * xx / width
        - 3 * yy / height
    )
    noise = rng.normal(0.0, 1.8, size=(height, width))
    arr = np.stack(
        [
            base + 4 + noise,
            base + noise,
            base - 8 + noise * 0.8,
        ],
        axis=2,
    )
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), mode="RGB")


def _glyph_lines(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    width: float,
    height: float,
    color: tuple[int, int, int],
    stroke: int,
    rng: random.Random,
) -> None:
    x1, y1 = x + width, y + height
    inset = max(1.0, stroke * 0.8)
    style = rng.randrange(0, 8)
    sw = max(1, int(round(stroke)))

    def line(a: float, b: float, c: float, d: float) -> None:
        draw.line((a, b, c, d), fill=color, width=sw)

    # Character-like skeleton. Full box frames are deliberately avoided so the
    # block reads as strokes, not as a filled illustration.
    if style in (0, 1, 4, 6):
        line(x + inset, y + height * 0.27, x1 - inset, y + height * 0.27)
    if style in (1, 2, 5, 7):
        line(x + inset, y + height * 0.50, x1 - inset, y + height * 0.50)
    if style in (0, 2, 3, 5):
        line(x + inset, y + height * 0.76, x1 - inset, y + height * 0.76)
    if style in (0, 2, 4, 7):
        line(x + width * 0.50, y + inset, x + width * 0.50, y1 - inset)
    if style in (1, 3, 4):
        line(x + width * 0.27, y + inset, x + width * 0.27, y + height * 0.58)
    if style in (2, 3, 6):
        line(x + width * 0.73, y + inset, x + width * 0.73, y + height * 0.62)
    if style in (3, 5):
        line(x + inset, y + height * 0.63, x + width * 0.45, y1 - inset)
    if style in (5, 6):
        line(x1 - inset, y + height * 0.62, x + width * 0.55, y1 - inset)
    if style == 7:
        line(x + width * 0.25, y1 - inset, x1 - inset, y + inset)


def _draw_body(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    ink: tuple[int, int, int],
    seed: int,
) -> None:
    x0, y0, x1, y1 = box
    rng = random.Random(seed ^ (x0 * 1009) ^ y0)
    width = x1 - x0
    glyph_w = width - 24
    glyph_h = 58
    pitch = 72
    count = max(4, (y1 - y0 - 18) // pitch)
    for i in range(count):
        gy = y0 + 10 + i * pitch
        if gy + glyph_h > y1 - 6:
            break
        jitter = rng.choice([-2, -1, 0, 0, 1, 2])
        _glyph_lines(
            draw,
            x0 + 12 + jitter,
            gy,
            glyph_w,
            glyph_h,
            ink,
            3,
            rng,
        )


def _draw_annotation(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    ink: tuple[int, int, int],
    seed: int,
) -> None:
    x0, y0, x1, y1 = box
    rng = random.Random(seed ^ (x0 * 1009) ^ (y0 * 7919))
    width = x1 - x0
    sub_w = max(16, (width - 16) // 2)
    gap = width - 2 * sub_w - 8
    glyph_h = 24
    pitch = 34
    count = max(4, (y1 - y0 - 8) // pitch)
    for row in range(count):
        gy = y0 + 5 + row * pitch
        if gy + glyph_h > y1 - 3:
            break
        for col in range(2):
            gx = x0 + 4 + col * (sub_w + gap)
            _glyph_lines(draw, gx, gy, sub_w, glyph_h, ink, 2, rng)


def _draw_header(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    ink: tuple[int, int, int],
    faded: tuple[int, int, int],
    seed: int,
) -> None:
    x0, y0, x1, y1 = box
    rng = random.Random(seed ^ 0x5A17)
    subcols = 3
    lane = (x1 - x0) // subcols
    glyph_h = 28
    pitch = 40
    count = max(3, (y1 - y0 - 10) // pitch)
    for col in range(subcols):
        cx = x0 + 8 + col * lane
        col_w = lane - 18
        for row in range(count):
            gy = y0 + 7 + row * pitch
            if gy + glyph_h > y1 - 4:
                break
            color = ink if (row + col) % 5 else faded
            _glyph_lines(draw, cx, gy, col_w, glyph_h, color, 2, rng)


def _draw_illustration(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    seed: int,
) -> None:
    x0, y0, x1, y1 = box
    rng = random.Random(seed ^ 0x13579)
    dark = (34, 37, 37)
    soft = (80, 76, 65)
    draw.rounded_rectangle((x0 + 3, y0 + 3, x1 - 3, y1 - 3), radius=6, outline=dark, width=3)
    # Mountains, path and a small pavilion: disconnected line art exercises
    # fragment merging without looking like text.
    for i in range(3):
        base_y = y0 + (y1 - y0) * (0.56 + i * 0.10)
        peak_x = x0 + (x1 - x0) * (0.25 + i * 0.24)
        peak_y = y0 + (y1 - y0) * (0.25 + i * 0.07)
        draw.line((x0 + 24, base_y, peak_x, peak_y, x1 - 24, base_y), fill=dark, width=4)
        draw.line((peak_x, peak_y, peak_x + 32, base_y - 28), fill=soft, width=2)
    draw.line(
        (
            x0 + (x1 - x0) * 0.20,
            y1 - 38,
            x0 + (x1 - x0) * 0.48,
            y0 + (y1 - y0) * 0.55,
            x0 + (x1 - x0) * 0.80,
            y1 - 38,
        ),
        fill=soft,
        width=3,
    )
    draw.rectangle((x0 + (x1 - x0) * 0.70, y0 + 52, x0 + (x1 - x0) * 0.88, y0 + 128), outline=dark, width=3)
    draw.line(
        (
            x0 + (x1 - x0) * 0.67,
            y0 + 94,
            x0 + (x1 - x0) * 0.84,
            y0 + 52,
            x0 + (x1 - x0) * 0.92,
            y0 + 94,
        ),
        fill=dark,
        width=3,
    )
    for i in range(9):
        px = x0 + 35 + rng.randrange(0, max(1, x1 - x0 - 70))
        py = y1 - 90 + rng.randrange(0, 45)
        draw.ellipse((px, py, px + 5, py + 4), fill=soft)


def _draw_seal(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    seed: int,
) -> None:
    x0, y0, x1, y1 = box
    rng = random.Random(seed ^ 0x2468)
    red = (155, 22, 20)
    light_red = (190, 52, 42)
    draw.rectangle((x0 + 5, y0 + 5, x1 - 5, y1 - 5), outline=red, width=8)
    draw.rectangle((x0 + 21, y0 + 21, x1 - 21, y1 - 21), outline=light_red, width=3)
    # Dense, square seal characters.
    cells = 2
    inner_x0, inner_y0 = x0 + 31, y0 + 31
    inner_w = (x1 - x0 - 62) / cells
    inner_h = (y1 - y0 - 62) / cells
    for row in range(cells):
        for col in range(cells):
            cx = inner_x0 + col * inner_w
            cy = inner_y0 + row * inner_h
            for k in range(4):
                pad = 6 + k * 5
                if k % 2 == 0:
                    draw.line((cx + 5, cy + pad, cx + inner_w - 5, cy + pad), fill=red, width=4)
                else:
                    draw.line((cx + pad, cy + 4, cx + pad, cy + inner_h - 4), fill=red, width=4)
    # Deterministic impression holes keep the color ratio from being uniform.
    for _ in range(12):
        px = x0 + 18 + rng.randrange(0, max(1, x1 - x0 - 36))
        py = y0 + 18 + rng.randrange(0, max(1, y1 - y0 - 36))
        draw.rectangle((px, py, px + 3, py + 3), fill=(226, 205, 174))
