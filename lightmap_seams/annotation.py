"""Annotated copy drawing.

Palette is part of the contract and is documented in the README:

* light-leak seam      -> pure red   ``(255, 0, 0)``
* out-of-bounds seam   -> pure blue  ``(0, 0, 255)``

Only these two colours are ever written, so a lightmap with no findings
produces an annotation containing no palette pixels.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

from .analysis import LEAK, OUT_OF_BOUNDS, SeamVerdict
from .seams import Seam

LEAK_COLOR = (255, 0, 0)
OUT_OF_BOUNDS_COLOR = (0, 0, 255)
LINE_WIDTH = 3
ENDPOINT_RADIUS = 4


def _clamp(value: float, maximum: int) -> int:
    # NaN cannot reach here (the seam parser rejects non-finite UVs), but a
    # defensive fallback keeps annotation of a hostile input from raising.
    if not math.isfinite(value):
        return 0 if math.isnan(value) else (maximum if value > 0 else 0)
    return min(max(int(round(value)), 0), maximum)


def _pixel_endpoints(seam: Seam, width: int, height: int) -> tuple[int, int, int, int]:
    x1 = _clamp(seam.u1 * (width - 1), width - 1)
    y1 = _clamp((1.0 - seam.v1) * (height - 1), height - 1)
    x2 = _clamp(seam.u2 * (width - 1), width - 1)
    y2 = _clamp((1.0 - seam.v2) * (height - 1), height - 1)
    return x1, y1, x2, y2


def _mark_endpoint(draw: ImageDraw.ImageDraw, x: int, y: int, color) -> None:
    draw.ellipse(
        (
            x - ENDPOINT_RADIUS,
            y - ENDPOINT_RADIUS,
            x + ENDPOINT_RADIUS,
            y + ENDPOINT_RADIUS,
        ),
        fill=color,
        outline=color,
    )


def _draw_overlay(draw: ImageDraw.ImageDraw, seam: Seam, width: int, height: int, color) -> None:
    x1, y1, x2, y2 = _pixel_endpoints(seam, width, height)
    draw.line((x1, y1, x2, y2), fill=color, width=LINE_WIDTH)
    _mark_endpoint(draw, x1, y1, color)
    _mark_endpoint(draw, x2, y2, color)


def annotate(
    image: Image.Image, verdicts: list[SeamVerdict], destination: Path
) -> int:
    """Write an annotated copy and return the number of drawn seams."""

    canvas = image.convert("RGB")
    draw = ImageDraw.Draw(canvas)
    width, height = canvas.size
    drawn = 0

    for verdict in verdicts:
        if verdict.classification == LEAK:
            _draw_overlay(draw, verdict.seam, width, height, LEAK_COLOR)
            drawn += 1
        elif verdict.classification == OUT_OF_BOUNDS:
            _draw_overlay(draw, verdict.seam, width, height, OUT_OF_BOUNDS_COLOR)
            drawn += 1

    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination, format="PNG")
    return drawn
