"""Deterministic local page-sample generator.

Each page is rendered from a fixed ``image_seed`` onto a fixed-size grayscale
canvas using a layout template.  Text lines are assembled by concatenating
deterministic stroke glyphs.  Every line carries its ground-truth bounding box
and ground-truth string.  A JSONL manifest records ``page_id``, ``image_seed``,
``image_shape``, ``line_count``, ``charset_version`` and ``policy``.

No external images, fonts or network resources are used.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .deterministic import content_hash, seeded_rng
from .templates import CHARSET, GLYPH_H, GLYPH_W, build_library

CANVAS_W = 120
CANVAS_H = 160
MARGIN_X = 6
MARGIN_Y = 8
LINE_GAP = 6
CELL_GAP = 1
BACKGROUND = 200
INK = 25


@dataclass(frozen=True)
class LineTruth:
    """Ground truth for one rendered text line."""

    line_index: int
    text: str
    bbox: Tuple[int, int, int, int]  # (x0, y0, x1, y1) inclusive-exclusive

    def to_dict(self) -> Dict[str, object]:
        return {
            "line_index": self.line_index,
            "text": self.text,
            "bbox": list(self.bbox),
        }


@dataclass
class PageSample:
    """A generated page: pixels plus ground-truth lines and metadata."""

    page_id: str
    image_seed: int
    image_shape: Tuple[int, int]
    charset_version: int
    policy: str
    image: np.ndarray
    lines: List[LineTruth]

    def manifest_record(self) -> Dict[str, object]:
        return {
            "page_id": self.page_id,
            "image_seed": self.image_seed,
            "image_shape": list(self.image_shape),
            "line_count": len(self.lines),
            "charset_version": self.charset_version,
            "policy": self.policy,
        }

    def pixel_hash(self) -> str:
        return content_hash(self.image.tolist())


def _random_text(rng: np.random.Generator, length: int) -> str:
    return "".join(str(CHARSET[int(rng.integers(0, len(CHARSET)))]) for _ in range(length))


def generate_page(
    page_id: str,
    image_seed: int,
    *,
    charset_version: int = 1,
    line_count: Optional[int] = None,
    policy: str = "standard",
    image_shape: Tuple[int, int] = (CANVAS_H, CANVAS_W),
) -> PageSample:
    """Render one deterministic page and its ground truth."""

    rng = seeded_rng("page", image_seed)
    library = build_library(charset_version)
    height, width = image_shape
    canvas = np.full((height, width), BACKGROUND, dtype=np.uint8)

    if line_count is None:
        line_count = int(rng.integers(4, 9))

    lines: List[LineTruth] = []
    y = MARGIN_Y
    line_pitch = GLYPH_H + LINE_GAP
    for line_index in range(line_count):
        if y + GLYPH_H > height - MARGIN_Y:
            break
        text_len = int(rng.integers(6, 12))
        text = _random_text(rng, text_len)
        x = MARGIN_X
        for char in text:
            glyph = library.glyph(char)
            # The stroke glyph uses 0/255; map ink onto the page's dark value
            # while keeping background untouched, deterministically.
            ink_mask = glyph > 0
            region = canvas[y : y + GLYPH_H, x : x + GLYPH_W]
            region[ink_mask] = INK
            x += GLYPH_W + CELL_GAP
            if x + GLYPH_W > width - MARGIN_X:
                break
        x0 = MARGIN_X
        y0 = y
        x1 = x - CELL_GAP
        y1 = y + GLYPH_H
        lines.append(LineTruth(line_index, text, (x0, y0, x1, y1)))
        y += line_pitch

    return PageSample(
        page_id=page_id,
        image_seed=image_seed,
        image_shape=(height, width),
        charset_version=charset_version,
        policy=policy,
        image=canvas,
        lines=lines,
    )


def write_dataset(
    root: str,
    seeds: Sequence[Tuple[str, int]],
    *,
    charset_version: int = 1,
    policy: str = "standard",
) -> List[PageSample]:
    """Generate pages and write manifest JSONL plus per-page pixel arrays.

    Returns the in-memory pages.  Layout under ``root``::

        manifest.jsonl
        pages/<page_id>.npz
        truth/<page_id>.json
    """

    pages_dir = os.path.join(root, "pages")
    truth_dir = os.path.join(root, "truth")
    os.makedirs(pages_dir, exist_ok=True)
    os.makedirs(truth_dir, exist_ok=True)

    pages: List[PageSample] = []
    manifest_path = os.path.join(root, "manifest.jsonl")
    with open(manifest_path, "w", encoding="utf-8") as manifest:
        for page_id, seed in seeds:
            page = generate_page(
                page_id, seed, charset_version=charset_version, policy=policy
            )
            np.savez_compressed(
                os.path.join(pages_dir, f"{page_id}.npz"), image=page.image
            )
            with open(
                os.path.join(truth_dir, f"{page_id}.json"), "w", encoding="utf-8"
            ) as handle:
                json.dump(
                    {
                        "page_id": page.page_id,
                        "lines": [line.to_dict() for line in page.lines],
                    },
                    handle,
                    sort_keys=True,
                )
            manifest.write(
                json.dumps(page.manifest_record(), sort_keys=True) + "\n"
            )
            pages.append(page)
    return pages


def load_manifest(root: str) -> List[Dict[str, object]]:
    """Parse manifest JSONL, raising a structured parse error on bad lines."""

    from .errors import ErrorCode, make_error

    path = os.path.join(root, "manifest.jsonl")
    records: List[Dict[str, object]] = []
    required = {
        "page_id",
        "image_seed",
        "image_shape",
        "line_count",
        "charset_version",
        "policy",
    }
    with open(path, "r", encoding="utf-8") as handle:
        for lineno, raw in enumerate(handle, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise make_error(
                    ErrorCode.PARSE_FAILED,
                    "manifest line is not valid JSON",
                    stage="manifest",
                    line_number=lineno,
                    raw=raw,
                ) from exc
            missing = sorted(required - set(record))
            if missing:
                raise make_error(
                    ErrorCode.PARSE_FAILED,
                    "manifest record missing required fields",
                    page_id=str(record.get("page_id")),
                    stage="manifest",
                    missing_fields=missing,
                    line_number=lineno,
                )
            records.append(record)
    return records


def load_page(root: str, record: Dict[str, object]) -> PageSample:
    """Load a page's pixels and ground truth from the dataset directory."""

    page_id = str(record["page_id"])
    with np.load(os.path.join(root, "pages", f"{page_id}.npz")) as data:
        image = data["image"].copy()
    with open(os.path.join(root, "truth", f"{page_id}.json"), "r", encoding="utf-8") as handle:
        truth = json.load(handle)
    lines = [
        LineTruth(
            line_index=int(item["line_index"]),
            text=str(item["text"]),
            bbox=tuple(item["bbox"]),  # type: ignore[arg-type]
        )
        for item in truth["lines"]
    ]
    shape = tuple(record["image_shape"])  # type: ignore[arg-type]
    return PageSample(
        page_id=page_id,
        image_seed=int(record["image_seed"]),
        image_shape=shape,  # type: ignore[arg-type]
        charset_version=int(record["charset_version"]),
        policy=str(record["policy"]),
        image=image,
        lines=lines,
    )
