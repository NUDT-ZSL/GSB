"""Preview rendering with Pillow, sharing the scene's world->pixel mapping."""

from __future__ import annotations

from PIL import Image, ImageDraw

from .contour import LevelContours, level_colors
from .scene import Scene

BACKGROUND = (255, 255, 255)


def render_preview(
    scene: Scene,
    contours: dict[float, LevelContours],
    markers: list[tuple[float, float]] | None = None,
) -> Image.Image:
    image = Image.new("RGB", (scene.canvas.width, scene.canvas.height), scene.background)
    draw = ImageDraw.Draw(image)
    colors = dict(zip(scene.levels, level_colors(len(scene.levels))))
    width = max(1, scene.line_width)

    for level in scene.levels:
        color = colors[level]
        for a, b in contours[level].segments:
            pa = scene.world_to_pixel(*a)
            pb = scene.world_to_pixel(*b)
            if pa == pb:
                # Isolated contour point (single-row/column grids): draw a dot.
                x, y = pa
                r = width / 2.0
                draw.ellipse([x - r, y - r, x + r, y + r], fill=color)
            else:
                draw.line([pa, pb], fill=color, width=width)

    if markers:
        radius = max(3, width + 2)
        for wx, wy in markers:
            if not scene.in_bounds(wx, wy):
                continue
            x, y = scene.world_to_pixel(wx, wy)
            draw.ellipse(
                [x - radius, y - radius, x + radius, y + radius],
                outline=(0, 0, 0),
                width=1,
            )
            draw.ellipse([x - 1, y - 1, x + 1, y + 1], fill=(0, 0, 0))
    return image


def pixel_of(scene: Scene, point: tuple[float, float]) -> tuple[float, float]:
    """Public helper so callers and tests can measure the same mapping."""
    return scene.world_to_pixel(*point)
