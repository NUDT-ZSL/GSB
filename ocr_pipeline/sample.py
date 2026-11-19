"""Deterministic document image generation.

Each page is a grayscale uint8 array rendered from fixed-seed RNG.  Text lines
are composed from template glyph slots at a fixed layout.  Every rendered line
also emits a ground-truth box (x1,y1,x2,y2) and the true string.  A small,
deterministic amount of salt noise is added *after* text so detection has real
work to do while remaining reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .templates import GLYPH_HEIGHT, GLYPH_WIDTH, TemplateLibrary
from .util import rng_from_seed

BG_VALUE = 235
INK_VALUE = 25
SALT_VALUE = 0
SALT_RATE = 0.0005
MARGIN_X = 14
MARGIN_Y = 16
LINE_GAP = 12
GLYPH_GAP = 2


@dataclass(frozen=True)
class GroundTruthLine:
    line_no: int
    text: str
    box: tuple[int, int, int, int]


@dataclass(frozen=True)
class PageSample:
    page_id: str
    image_seed: int
    image: np.ndarray
    shape: tuple[int, int]
    truth: list[GroundTruthLine]
    line_count: int

    def to_json(self) -> dict:
        return {
            "page_id": self.page_id,
            "image_seed": self.image_seed,
            "image_shape": list(self.shape),
            "line_count": self.line_count,
            "ground_truth": [
                {"line_no": line.line_no, "text": line.text, "box": list(line.box)}
                for line in self.truth
            ],
        }


def _render_glyph(
    image: np.ndarray, x: int, y: int, bitmap: np.ndarray,
) -> None:
    region = image[y:y + GLYPH_HEIGHT, x:x + GLYPH_WIDTH]
    # Fixed ink value: glyph shape alone (from the deterministic template)
    # drives recognition, while separate salt noise still challenges detection.
    region[bitmap.astype(bool)] = INK_VALUE


def generate_page(
    page_id: str,
    image_seed: int,
    library: TemplateLibrary,
    shape: tuple[int, int] = (120, 160),
    line_count: int = 4,
) -> PageSample:
    height, width = shape
    rng = rng_from_seed(image_seed, height, width, line_count, library.library_version)
    image = np.full((height, width), BG_VALUE, dtype=np.uint8)
    truth: list[GroundTruthLine] = []

    max_chars = (width - 2 * MARGIN_X + GLYPH_GAP) // (GLYPH_WIDTH + GLYPH_GAP)
    if max_chars < 3:
        raise ValueError("page too narrow for deterministic layout")

    for line_no in range(line_count):
        y = MARGIN_Y + line_no * (GLYPH_HEIGHT + LINE_GAP)
        if y + GLYPH_HEIGHT > height - MARGIN_Y:
            raise ValueError("page too short for requested line count")
        # Deterministic per-line length and character choices.
        line_rng = rng_from_seed(image_seed, line_no + 1, library.glyph_count)
        char_count = 3 + int(line_rng.integers(0, min(5, max_chars - 2)))
        char_indices = line_rng.integers(0, library.glyph_count, size=char_count)
        text = "".join(library.alphabet[int(index)] for index in char_indices)

        x = MARGIN_X
        for glyph_index in char_indices:
            bitmap = library.templates[int(glyph_index)]
            _render_glyph(image, x, y, bitmap)
            x += GLYPH_WIDTH + GLYPH_GAP
        # After the loop x points one gap past the final glyph; last ink column
        # is x - GLYPH_GAP - 1.
        box = (MARGIN_X, y, x - GLYPH_GAP - 1, y + GLYPH_HEIGHT - 1)
        truth.append(GroundTruthLine(line_no=line_no, text=text, box=box))

    # Deterministic post-render salt noise.
    noise_rng = rng_from_seed(image_seed, 0x5A17, height * width)
    noise_mask = noise_rng.random((height, width)) < SALT_RATE
    image[noise_mask] = SALT_VALUE

    return PageSample(
        page_id=page_id,
        image_seed=image_seed,
        image=image,
        shape=(height, width),
        truth=truth,
        line_count=line_count,
    )


def build_task_row(
    page: PageSample, charset_version: str, policy: str
) -> dict:
    return {
        "page_id": page.page_id,
        "image_seed": page.image_seed,
        "image_shape": list(page.shape),
        "line_count": page.line_count,
        "charset_version": charset_version,
        "policy": policy,
    }
