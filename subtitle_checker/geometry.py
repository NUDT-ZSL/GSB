"""Deterministic text and rectangle geometry.

Text is modelled as a block of lines stacked vertically, matching how the
editing software lays out unwrapped subtitles:

* each line's width is the sum of its character advances
  (full-width / East Asian characters advance 1.00 × font size,
   everything else 0.60 × font size, combining marks advance 0);
* line height = round(font_size × 1.20);
* block width = widest line; block height = line height × line count.

The anchor is the block's centre. All edges derive from one integer top-left
corner, so results are identical on every run.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass

FULL_WIDTH_RATIO = 1.00
HALF_WIDTH_RATIO = 0.60
LINE_HEIGHT_RATIO = 1.20


def normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def char_advance(char: str, font_size: float) -> float:
    if unicodedata.combining(char):
        return 0.0
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return font_size * FULL_WIDTH_RATIO
    return font_size * HALF_WIDTH_RATIO


def line_width(line: str, font_size: float) -> int:
    total = sum(char_advance(ch, font_size) for ch in line)
    return max(1, int(total + 0.5))


@dataclass(frozen=True)
class TextMetrics:
    """Measured geometry of a text block."""

    width: int
    height: int
    line_height: int
    line_widths: tuple[int, ...]
    line_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "width": self.width,
            "height": self.height,
            "line_height": self.line_height,
            "line_widths": list(self.line_widths),
            "line_count": self.line_count,
        }


def measure_text(text: str, font_size: float) -> TextMetrics:
    """Measure a possibly multiline subtitle block at the given font size."""
    lines = normalize_text(text).split("\n")
    if not lines:
        lines = [""]
    line_height = max(1, int(font_size * LINE_HEIGHT_RATIO + 0.5))
    widths = tuple(line_width(line, font_size) for line in lines)
    return TextMetrics(
        width=max(widths),
        height=line_height * len(lines),
        line_height=line_height,
        line_widths=widths,
        line_count=len(lines),
    )


@dataclass(frozen=True)
class Box:
    """Integer bounding box with exclusive right/bottom edges."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    def as_dict(self) -> dict[str, int]:
        return {"left": self.left, "top": self.top, "right": self.right, "bottom": self.bottom}


def text_box(anchor_x: float, anchor_y: float, metrics: TextMetrics) -> Box:
    """Centre the measured block on the anchor and snap to integer pixels."""
    left = int(math.floor(anchor_x - metrics.width / 2.0))
    top = int(math.floor(anchor_y - metrics.height / 2.0))
    return Box(left=left, top=top, right=left + metrics.width, bottom=top + metrics.height)


@dataclass(frozen=True)
class Overflow:
    """Amount a box escapes the safe area on each edge, in pixels."""

    top: int = 0
    right: int = 0
    bottom: int = 0
    left: int = 0

    @property
    def total(self) -> int:
        return self.top + self.right + self.bottom + self.left

    @property
    def triggered(self) -> bool:
        return self.total > 0

    def directions(self) -> list[str]:
        """Directions with a positive escape, largest pixel amount first."""
        pairs = [("top", self.top), ("right", self.right), ("bottom", self.bottom), ("left", self.left)]
        order = {"top": 0, "right": 1, "bottom": 2, "left": 3}
        return [name for name, amount in sorted(pairs, key=lambda p: (-p[1], order[p[0]])) if amount > 0]

    def as_dict(self) -> dict[str, int]:
        return {"top": self.top, "right": self.right, "bottom": self.bottom, "left": self.left}


def safe_area(canvas_width: int, canvas_height: int, margin: object) -> Box:
    """Return the safe rectangle as a Box (exclusive right/bottom)."""
    return Box(
        left=margin.left,  # type: ignore[attr-defined]
        top=margin.top,  # type: ignore[attr-defined]
        right=canvas_width - margin.right,  # type: ignore[attr-defined]
        bottom=canvas_height - margin.bottom,  # type: ignore[attr-defined]
    )


def overflow_against(box: Box, safe: Box) -> Overflow:
    return Overflow(
        top=max(0, safe.top - box.top),
        right=max(0, box.right - safe.right),
        bottom=max(0, box.bottom - safe.bottom),
        left=max(0, safe.left - box.left),
    )


def intersection_area(a: Box, b: Box) -> int:
    width = min(a.right, b.right) - max(a.left, b.left)
    height = min(a.bottom, b.bottom) - max(a.top, b.top)
    if width <= 0 or height <= 0:
        return 0
    return width * height


def occlusion_ratio(box: Box, fx: Box) -> float:
    """Fraction of ``box``'s area covered by ``fx`` (0.0 when box has no area)."""
    area = box.area
    if area <= 0:
        return 0.0
    return intersection_area(box, fx) / area
