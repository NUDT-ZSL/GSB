#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate deterministic local samples and acceptance-test scenes.

Run:  python tools/make_samples.py
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
TEXTURES = SAMPLES / "textures"
CASES = SAMPLES / "cases"


def make_texture(
    name: str, size: tuple[int, int], base: tuple[int, int, int],
    border: tuple[int, int, int], accent: tuple[int, int, int],
) -> None:
    image = Image.new("RGB", size, base)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, size[0] - 1, size[1] - 1), outline=border, width=4)
    width, height = size
    draw.rectangle(
        (width // 2 - 12, height // 2 - 8, width // 2 + 12, height // 2 + 8),
        fill=accent,
    )
    image.save(TEXTURES / name, format="PNG")


def make_veil_texture() -> None:
    """半透明横向渐变遮罩：用于验证“遮挡程度”是逐像素计算的。"""
    size = (96, 72)
    image = Image.new("RGBA", size, (40, 40, 60, 0))
    pixels = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            alpha = int(round(255 * x / (size[0] - 1)))
            pixels[x, y] = (40, 40, 60, alpha)
    image.save(TEXTURES / "veil.png", format="PNG")


def make_textures() -> None:
    TEXTURES.mkdir(parents=True, exist_ok=True)
    make_texture("back.png", (96, 72), (208, 138, 60), (120, 78, 34), (240, 196, 120))
    make_texture("middle.png", (88, 64), (74, 158, 107), (36, 88, 60), (140, 210, 160))
    make_texture("front.png", (64, 96), (179, 74, 74), (96, 36, 36), (230, 140, 120))
    make_veil_texture()


def write_scene(name: str, scene: dict) -> None:
    CASES.mkdir(parents=True, exist_ok=True)
    text = json.dumps(scene, ensure_ascii=False, indent=2) + "\n"
    (CASES / name).write_text(text, encoding="utf-8", newline="\n")


def base_meshes() -> list[dict]:
    return [
        {"name": "back", "layer": 1, "x": 8, "y": 48, "texture": "../textures/back.png"},
        {"name": "middle", "layer": 2, "x": 64, "y": 24, "texture": "../textures/middle.png"},
        {"name": "front", "layer": 3, "x": 120, "y": 16, "texture": "../textures/front.png"},
    ]


def base_scene() -> dict:
    return {
        "canvas": {"width": 240, "height": 160, "background": "#202830"},
        "occlusion_strength": 0.65,
        "meshes": base_meshes(),
    }


def copy_scene(mutator) -> dict:
    scene = base_scene()
    mutator(scene)
    return scene


def make_cases() -> None:
    write_scene("base.json", base_scene())

    # 需求 1：缺少层级顺序字段的网格。
    def drop_layer(scene: dict) -> None:
        scene["meshes"].append(
            {"name": "no_layer", "x": 4, "y": 4, "texture": "../textures/back.png"}
        )

    write_scene("missing_layer.json", copy_scene(drop_layer))

    # 需求 4：中间层贴图缺失。
    def drop_texture(scene: dict) -> None:
        scene["meshes"][1]["texture"] = "../textures/missing_middle.png"

    write_scene("missing_texture.json", copy_scene(drop_texture))

    # 需求 5：最前层部分移出画布右边界。
    def clip_front(scene: dict) -> None:
        scene["meshes"][2]["x"] = 220

    write_scene("clipped.json", copy_scene(clip_front))

    # 需求 6：画布宽度为 0 时拒绝整个场景。
    def zero_canvas(scene: dict) -> None:
        scene["canvas"]["width"] = 0

    write_scene("canvas_zero.json", copy_scene(zero_canvas))

    # 需求 6：个别网格坐标非法，其余照常合成。
    def bad_coord(scene: dict) -> None:
        scene["meshes"].append(
            {
                "name": "bad_coord",
                "layer": 4,
                "x": "abc",
                "y": 130,
                "texture": "../textures/middle.png",
            }
        )

    write_scene("bad_mesh.json", copy_scene(bad_coord))

    # 需求 3：半透明前层，遮挡程度随像素变化；被完全遮挡的基准网格逐点变暗。
    write_scene(
        "partial_occlusion.json",
        {
            "canvas": {"width": 200, "height": 100, "background": "#000000"},
            "occlusion_strength": 0.65,
            "meshes": [
                {
                    "name": "base",
                    "layer": 1,
                    "x": 10,
                    "y": 10,
                    "texture": "../textures/back.png",
                },
                {
                    "name": "veil",
                    "layer": 2,
                    "x": 10,
                    "y": 10,
                    "texture": "../textures/veil.png",
                },
            ],
        },
    )

    # 需求 7：成功、缺贴图、坐标非法三类网格。
    write_scene(
        "mixed.json",
        {
            "canvas": {"width": 160, "height": 120, "background": "#334455"},
            "meshes": [
                {
                    "name": "ok_mesh",
                    "layer": 1,
                    "x": 10,
                    "y": 10,
                    "texture": "../textures/back.png",
                },
                {
                    "name": "missing_mesh",
                    "layer": 2,
                    "x": 100,
                    "y": 20,
                    "texture": "../textures/nope.png",
                },
                {
                    "name": "bad_coord",
                    "layer": 3,
                    "x": "abc",
                    "y": 30,
                    "texture": "../textures/middle.png",
                },
            ],
        },
    )


def main() -> int:
    make_textures()
    make_cases()
    print(f"贴图目录: {TEXTURES}")
    print(f"场景目录: {CASES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
