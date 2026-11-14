"""Deterministically generate the sample nine-patch manifest and textures.

The output is written to ``samples/`` next to this script:

* ``textures/*.png`` -- RGB/RGBA textures with a transparent or solid border
* ``manifest.json`` -- ordered entries, paths relative to the manifest
* ``broken_manifest.json`` -- same entries but with two deliberately
  invalid ones, used to exercise the failure paths
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw

SAMPLE_DIR = Path(__file__).resolve().parent / "samples"
TEXTURE_DIR = SAMPLE_DIR / "textures"


def _checker(size: tuple[int, int], cell: int, light: tuple, dark: tuple) -> Image.Image:
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(size[1]):
        for x in range(size[0]):
            if (x // cell + y // cell) % 2 == 0:
                draw.point((x, y), fill=light)
            else:
                draw.point((x, y), fill=dark)
    return image


def _panel_texture(name: str, size: tuple[int, int], border: int, seed: int) -> Image.Image:
    """A rounded-looking panel with visibly distinct border and centre pixels."""

    rng = random.Random(seed)
    image = _checker(size, 4, (36, 40, 48), (44, 49, 58))
    draw = ImageDraw.Draw(image)

    # Outer border (the nine-patch corner + edge material).
    draw.rectangle((0, 0, size[0] - 1, size[1] - 1), outline=(225, 168, 72), width=border)
    # Inner highlight so stretching along an edge is obvious.
    draw.rectangle(
        (border, border, size[0] - 1 - border, size[1] - 1 - border),
        outline=(255, 214, 138),
        width=1,
    )
    # Centre decoration: must be stretched in both directions.
    centre_box = (
        border + 2,
        border + 2,
        size[0] - 1 - border - 2,
        size[1] - 1 - border - 2,
    )
    draw.ellipse(centre_box, outline=(90, 200, 255), width=2)
    draw.line(centre_box, fill=(90, 200, 255), width=1)
    for _ in range(12):
        x = rng.randint(border, max(border, size[0] - border - 1))
        y = rng.randint(border, max(border, size[1] - border - 1))
        draw.point((x, y), fill=(255, 255, 255))
    return image


def _transparent_icon(size: tuple[int, int], border: int) -> Image.Image:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, size[0] - 1, size[1] - 1),
        radius=min(size) // 4,
        fill=(255, 60, 60, 255),
        outline=(255, 255, 255, 255),
        width=border,
    )
    draw.ellipse(
        (border + 3, border + 3, size[0] - 1 - border - 3, size[1] - 1 - border - 3),
        fill=(255, 255, 255, 200),
    )
    return image


def build_entries() -> list[dict]:
    """The canonical ordered sample set covering every behaviour."""

    return [
        {
            "texture_name": "panel_blue",
            "image_path": "textures/panel_blue.png",
            "left": 16, "right": 16, "top": 16, "bottom": 16,
            "target_width": 240, "target_height": 120,
        },
        {
            "texture_name": "panel_green_small_border",
            "image_path": "textures/panel_green_small_border.png",
            "left": 4, "right": 4, "top": 4, "bottom": 4,
            "target_width": 180, "target_height": 72,
        },
        {
            "texture_name": "button_wide",
            "image_path": "textures/button_wide.png",
            "left": 12, "right": 12, "top": 8, "bottom": 8,
            "target_width": 320, "target_height": 64,
        },
        {
            "texture_name": "icon_transparent",
            "image_path": "textures/icon_transparent.png",
            "left": 18, "right": 18, "top": 18, "bottom": 18,
            "target_width": 150, "target_height": 150,
        },
        {
            "texture_name": "edge_zero_left",
            "image_path": "textures/edge_zero_left.png",
            "left": 0, "right": 10, "top": 6, "bottom": 6,
            "target_width": 128, "target_height": 48,
        },
        {
            "texture_name": "edge_zero_top_bottom",
            "image_path": "textures/edge_zero_top_bottom.png",
            "left": 8, "right": 8, "top": 0, "bottom": 0,
            "target_width": 96, "target_height": 64,
        },
        {
            "texture_name": "zero_all_edges",
            "image_path": "textures/zero_all_edges.png",
            "left": 0, "right": 0, "top": 0, "bottom": 0,
            "target_width": 80, "target_height": 80,
        },
        {
            "texture_name": "compress_horizontal",
            "image_path": "textures/compress_horizontal.png",
            "left": 40, "right": 40, "top": 8, "bottom": 8,
            "target_width": 60, "target_height": 60,
        },
        {
            "texture_name": "compress_vertical",
            "image_path": "textures/compress_vertical.png",
            "left": 8, "right": 8, "top": 32, "bottom": 32,
            "target_width": 64, "target_height": 48,
        },
        {
            "texture_name": "compress_tiny_target",
            "image_path": "textures/compress_tiny_target.png",
            "left": 60, "right": 60, "top": 20, "bottom": 20,
            "target_width": 4, "target_height": 8,
        },
        {
            "texture_name": "no_downscale_needed",
            "image_path": "textures/no_downscale_needed.png",
            "left": 10, "right": 10, "top": 10, "bottom": 10,
            "target_width": 64, "target_height": 64,
        },
    ]


def build_broken_entries(base: list[dict]) -> list[dict]:
    """Sample entries that must fail, interleaved with valid ones."""

    broken = []

    def copy_of(name: str, **overrides):
        entry = next(item for item in base if item["texture_name"] == name).copy()
        entry.update(overrides)
        return entry

    broken.append(copy_of("panel_blue"))
    # 1) missing image file
    broken.append(copy_of(
        "button_wide",
        texture_name="missing_image",
        image_path="textures/does_not_exist.png",
    ))
    # 2) negative margin
    broken.append(copy_of(
        "compress_horizontal",
        texture_name="negative_left",
        left=-1,
    ))
    # 3) margins larger than the source image
    broken.append(copy_of(
        "panel_green_small_border",
        texture_name="margin_too_wide",
        left=100, right=100,
    ))
    # 4) undecodable file
    broken.append(copy_of(
        "panel_green_small_border",
        texture_name="corrupt_image",
        image_path="textures/corrupt.png",
    ))
    # 5) zero-all still succeeds after four failures
    broken.append(copy_of("zero_all_edges"))
    return broken


def write_textures() -> None:
    TEXTURE_DIR.mkdir(parents=True, exist_ok=True)
    images = {
        "panel_blue": _panel_texture("panel_blue", (128, 128), 16, 1071),
        "panel_green_small_border": _panel_texture(
            "panel_green_small_border", (96, 96), 4, 1072
        ),
        "button_wide": _panel_texture("button_wide", (192, 64), 8, 1073),
        "icon_transparent": _transparent_icon((96, 96), 18),
        "edge_zero_left": _panel_texture("edge_zero_left", (128, 64), 10, 1074),
        "edge_zero_top_bottom": _panel_texture(
            "edge_zero_top_bottom", (96, 64), 8, 1075
        ),
        "zero_all_edges": _checker((64, 64), 8, (30, 120, 200), (20, 80, 160)),
        "compress_horizontal": _panel_texture("compress_horizontal", (128, 128), 40, 1076),
        "compress_vertical": _panel_texture("compress_vertical", (96, 128), 32, 1077),
        "compress_tiny_target": _panel_texture(
            "compress_tiny_target", (192, 96), 20, 1078
        ),
        "no_downscale_needed": _panel_texture("no_downscale_needed", (96, 96), 10, 1079),
    }
    for name, image in images.items():
        image.save(TEXTURE_DIR / f"{name}.png", format="PNG")

    # A file with a .png extension that Pillow cannot decode.
    (TEXTURE_DIR / "corrupt.png").write_bytes(b"this is not a PNG file\n")


def write_manifests(entries: list[dict], broken: list[dict]) -> None:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    for filename, data in (
        ("manifest.json", entries),
        ("broken_manifest.json", broken),
    ):
        with (SAMPLE_DIR / filename).open("w", encoding="utf-8") as handle:
            json.dump({"textures": data}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")


def main() -> int:
    entries = build_entries()
    write_textures()
    write_manifests(entries, build_broken_entries(entries))
    print(f"已生成样本: {len(entries)} 项纹理, 清单 {SAMPLE_DIR / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
