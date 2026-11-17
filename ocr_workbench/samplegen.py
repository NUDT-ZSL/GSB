"""Deterministic synthetic layout generator.

Renders horizontal text bands and isolated character blocks with the built-in
5x7 bitmap font on a light grayscale background, adds seeded salt-and-pepper
noise, and writes a PNG plus a layout ground-truth JSON per image.  A fixed
seed makes every byte of the output reproducible.
"""

import json
import os
import random

import numpy as np
from PIL import Image

from .font import CHARSET, FONT_VERSION, render_glyph

WIDTH = 480
HEIGHT = 320
BG = 245
FG = 25
SPACE = " "


def _random_text(rng, length):
    chars = []
    for idx in range(length):
        if idx > 0 and rng.random() < 0.10:
            chars.append(SPACE)
        else:
            chars.append(rng.choice(CHARSET))
    return "".join(chars)


def _draw_tile(canvas, tile, top, left):
    tile_h = len(tile)
    tile_w = len(tile[0])
    region = canvas[top:top + tile_h, left:left + tile_w]
    tile_arr = np.asarray(tile, dtype=np.uint8)
    mask = tile_arr < 200
    region[mask] = tile_arr[mask]
    canvas[top:top + tile_h, left:left + tile_w] = region


def _add_noise(canvas, rng, count):
    for _ in range(count):
        y = rng.randrange(canvas.shape[0])
        x = rng.randrange(canvas.shape[1])
        canvas[y, x] = rng.choice((0, 255))


def generate_image(seed, scale=2, line_count=4, noise_count=40):
    rng = random.Random(seed)
    canvas = np.full((HEIGHT, WIDTH), BG, dtype=np.uint8)
    lines = []
    pitch_y = HEIGHT // (line_count + 1)
    pitch_x = 5 * scale + scale

    for line_idx in range(line_count):
        top = 20 + line_idx * pitch_y + rng.randrange(0, 6)
        left = 24 + rng.randrange(0, 12)
        max_chars = (WIDTH - left - 16) // pitch_x
        text = _random_text(rng, rng.randrange(max_chars - 6, max_chars))
        chars = []
        cursor = left
        for ch in text:
            if ch == SPACE:
                box = [cursor, top, cursor + pitch_x, top + 7 * scale]
                chars.append({"char": SPACE, "bbox": box, "is_space": True})
                cursor += pitch_x
                continue
            tile = render_glyph(ch, scale=scale, fg=FG, bg=BG)
            _draw_tile(canvas, tile, top, cursor)
            box = [cursor, top, cursor + 5 * scale, top + 7 * scale]
            chars.append({"char": ch, "bbox": box, "is_space": False})
            cursor += pitch_x
        line_bbox = [left, top, cursor - scale, top + 7 * scale]
        lines.append({
            "line_index": line_idx,
            "text": text,
            "bbox": line_bbox,
            "chars": chars,
        })

    block_count = 2
    block_size = 3
    block_top = HEIGHT - 7 * scale - 10
    block_cursor = 24
    blocks = []
    for block_idx in range(block_count):
        text = "".join(rng.choice(CHARSET) for _ in range(block_size))
        chars = []
        for ch in text:
            tile = render_glyph(ch, scale=scale, fg=FG, bg=BG)
            _draw_tile(canvas, tile, block_top, block_cursor)
            chars.append({
                "char": ch,
                "bbox": [block_cursor, block_top,
                         block_cursor + 5 * scale, block_top + 7 * scale],
                "is_space": False,
            })
            block_cursor += pitch_x
        blocks.append({
            "block_index": block_idx,
            "text": text,
            "bbox": [chars[0]["bbox"][0], block_top,
                     block_cursor - scale, block_top + 7 * scale],
            "chars": chars,
        })
        block_cursor += 3 * pitch_x

    _add_noise(canvas, rng, noise_count)
    truth = {
        "font_version": FONT_VERSION,
        "image_size": [WIDTH, HEIGHT],
        "seed": seed,
        "scale": scale,
        "lines": lines,
        "blocks": blocks,
    }
    return canvas, truth


def generate_dataset(out_dir, image_count=6, base_seed=20261117, scale=2):
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for idx in range(image_count):
        seed = base_seed + idx * 97
        canvas, truth = generate_image(seed, scale=scale)
        name = f"layout_{idx:03d}"
        png_path = os.path.join(out_dir, name + ".png")
        json_path = os.path.join(out_dir, name + ".json")
        Image.fromarray(canvas, mode="L").save(png_path, format="PNG")
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(truth, handle, ensure_ascii=False, indent=2,
                      sort_keys=True)
        written.append((png_path, json_path))
    return written
