"""Per-subtitle preview rendering.

Each renderable subtitle gets one PNG whose canvas matches the manifest canvas.
The preview overlays:

* the FX region as a solid tinted band with a border;
* the safe-area border as a 2 px solid green frame;
* the measured text block, tinted by the entry's report group, with the
  subtitle text drawn at the manifest's font size.

Byte-identical across runs on the same machine: fixed drawing order, fixed
colours, integer coordinates and a fixed PNG encoder level.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .check import SubtitleResult
from .manifest import Manifest

BACKGROUND = (16, 16, 20, 255)
SAFE_BORDER = (0, 230, 118)
SAFE_BORDER_WIDTH = 2
FX_FILL = (58, 20, 24, 255)
FX_BORDER = (192, 57, 43, 255)
FX_BORDER_WIDTH = 2
TEXT_COLORS = {
    "pass": (79, 195, 247, 255),
    "overflow": (255, 82, 82, 255),
    "occlusion": (255, 179, 0, 255),
    "unverified": (158, 158, 158, 255),
}
TEXT_FILL_ALPHA = 70
GLYPH_COLOR = (245, 245, 245, 255)
LEGEND_BAND = (0, 0, 0, 110)

FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
FONT_CACHE: dict[int, ImageFont.FreeTypeFont | None] = {}


def _font(size: int) -> ImageFont.FreeTypeFont | None:
    if size in FONT_CACHE:
        return FONT_CACHE[size]
    font: ImageFont.FreeTypeFont | None = None
    for candidate in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(candidate, size=max(8, int(size)))
            break
        except OSError:
            continue
    FONT_CACHE[size] = font
    return font


def preview_filename(subtitle_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in subtitle_id)
    return f"subtitle_{safe}.png"


def _blend(base: tuple[int, int, int, int], top: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    alpha = top[3] / 255.0
    return (
        int(round(top[0] * alpha + base[0] * (1 - alpha))),
        int(round(top[1] * alpha + base[1] * (1 - alpha))),
        int(round(top[2] * alpha + base[2] * (1 - alpha))),
        255,
    )


def _clip_box(left: int, top: int, right: int, bottom: int, width: int, height: int) -> tuple[int, int, int, int] | None:
    l = max(0, min(left, width))
    t = max(0, min(top, height))
    r = max(0, min(right, width))
    b = max(0, min(bottom, height))
    if r <= l or b <= t:
        return None
    return l, t, r, b


def render_preview(result: SubtitleResult, manifest: Manifest) -> Image.Image:
    width = manifest.canvas.width
    height = manifest.canvas.height
    img = Image.new("RGBA", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(img, "RGBA")

    # FX region.
    fx = manifest.fx_region
    fx_box = _clip_box(fx.x, fx.y, fx.x + fx.width, fx.y + fx.height, width, height)
    if fx_box is not None:
        draw.rectangle(fx_box, fill=FX_FILL, outline=FX_BORDER, width=FX_BORDER_WIDTH * 2)

    # Safe-area border (2 px, exact colour, inside the boundary).
    margin = manifest.margin
    safe_left, safe_top = margin.left, margin.top
    safe_right = width - margin.right
    safe_bottom = height - margin.bottom
    for offset in range(SAFE_BORDER_WIDTH):
        l = safe_left + offset
        t = safe_top + offset
        r = safe_right - 1 - offset
        b = safe_bottom - 1 - offset
        if r < l or b < t:
            break
        draw.rectangle([l, t, r, b], outline=SAFE_BORDER)

    # Measured text block.
    if result.box is not None:
        box = result.box
        color = TEXT_COLORS.get(result.group, TEXT_COLORS["pass"])
        fill = _blend((0, 0, 0, 255), (color[0], color[1], color[2], TEXT_FILL_ALPHA))
        clipped = _clip_box(box.left, box.top, box.right, box.bottom, width, height)
        if clipped is not None:
            draw.rectangle(clipped, fill=fill, outline=color, width=2)
            for line_index, line in enumerate(result.subtitle.text.split("\n")):
                line_top = box.top + line_index * (result.metrics.line_height if result.metrics else 0)
                font = _font(result.subtitle.font_size)
                if font is None:
                    break
                try:
                    line_w = int(draw.textlength(line, font=font))
                except (ValueError, OSError):
                    line_w = 0
                x = box.left + (box.width - line_w) // 2
                y = line_top + ((result.metrics.line_height - _glyph_height(font)) // 2 if result.metrics else 0)
                draw.text((x, y), line, font=font, fill=GLYPH_COLOR)

    return img.convert("RGB")


def _glyph_height(font: ImageFont.FreeTypeFont) -> int:
    ascent, descent = font.getmetrics()
    return ascent + descent


def write_preview(result: SubtitleResult, manifest: Manifest, previews_dir: Path) -> Path:
    previews_dir.mkdir(parents=True, exist_ok=True)
    path = previews_dir / preview_filename(result.id)
    img = render_preview(result, manifest)
    img.save(path, format="PNG", compress_level=6)
    return path


def clear_stale_previews(previews_dir: Path, keep: set[str]) -> list[str]:
    """Remove preview PNGs from earlier runs that this run did not produce."""
    removed: list[str] = []
    if not previews_dir.is_dir():
        return removed
    for path in sorted(previews_dir.glob("subtitle_*.png")):
        if path.name not in keep:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                pass
    return removed
