"""Generate deterministic sample manifests and PNG layers for the compositor.

Every sample is written under ``<root>/<name>/`` with a ``manifest.json`` and a
``layers/`` directory (``<root>`` defaults to this script's directory).  PNGs are
created pixel-by-pixel from a fixed seed, so the files can be regenerated
bit-for-bit on any machine::

    python samples/generate_samples.py
    python samples/generate_samples.py --root /tmp/sample-copy

Total output stays well below 10 MB (the generated samples are ~60 KB).
"""

from __future__ import annotations

import argparse
import json
import os
import random
from typing import Any

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = HERE
SEED = 20240517
_seed_offset = 0


def _rng(salt: int) -> random.Random:
    return random.Random(SEED + salt + _seed_offset)


def _new_layer(width: int, height: int) -> Image.Image:
    return Image.new("RGBA", (width, height), (0, 0, 0, 0))


def _noisy_alpha_blob(width: int, height: int, salt: int,
                      color: tuple[int, int, int],
                      margin: int = 0) -> Image.Image:
    """A soft-edged rectangle filled with deterministic alpha noise."""
    rng = _rng(salt)
    img = _new_layer(width, height)
    draw = ImageDraw.Draw(img)
    left, top = margin, margin
    right, bottom = width - margin - 1, height - margin - 1
    draw.rectangle([left, top, right, bottom], fill=color + (235,))
    px = img.load()
    for y in range(top, bottom + 1):
        for x in range(left, right + 1):
            if rng.random() < 0.12:
                px[x, y] = (color[0], color[1], color[2], 0)
    return img


def _solid(width: int, height: int, color: tuple[int, int, int, int]) -> Image.Image:
    return Image.new("RGBA", (width, height), color)


def _write_sample(name: str, manifest: dict[str, Any],
                  layers: dict[str, Image.Image]) -> None:
    root = os.path.join(ROOT, name)
    layer_dir = os.path.join(root, "layers")
    os.makedirs(layer_dir, exist_ok=True)
    for layer_id, image in layers.items():
        image.save(os.path.join(layer_dir, f"{layer_id}.png"), format="PNG")
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"sample {name}: {len(layers)} layer image(s)")


# ---------------------------------------------------------------------------
# 1. order stacking
# ---------------------------------------------------------------------------


def sample_order() -> None:
    # Different footprints so the stacking result is visible as concentric
    # rings: b (largest) at the bottom, a in the middle, c on top.
    layers = {
        "a": _solid(40, 40, (220, 40, 40, 255)),
        "b": _solid(60, 60, (40, 160, 220, 255)),
        "c": _solid(20, 20, (240, 200, 30, 255)),
    }
    manifest = {
        "canvasWidth": 80,
        "canvasHeight": 80,
        "layers": [
            {"id": "c", "order": 3, "blendMode": "normal", "opacity": 100,
             "align": "center"},
            {"id": "b", "order": 1, "blendMode": "normal", "opacity": 100,
             "align": "center"},
            {"id": "a", "order": 2, "blendMode": "normal", "opacity": 100,
             "align": "center"},
        ],
    }
    _write_sample("order", manifest, layers)


# ---------------------------------------------------------------------------
# 2. blend modes
# ---------------------------------------------------------------------------

MUL_PX = (128, 255, 64, 255)    # multiply source


def sample_blend() -> None:
    # 60x40 canvas; four 30x20 quadrants of base colour plus opaque overlay
    # patches of 20x20 at (0,0) [multiply] and (20,0) [unknown -> normal].
    base = _new_layer(60, 40)
    quadrants = [(200, 120, 60, 255), (238, 136, 68, 255),
                 (90, 180, 210, 160), (60, 60, 60, 255)]
    for index, color in enumerate(quadrants):
        base.paste(Image.new("RGBA", (30, 20), color), ((index % 2) * 30,
                                                        (index // 2) * 20))
    mult = _solid(20, 20, MUL_PX)
    unknown = _solid(20, 20, (10, 200, 120, 255))
    manifest = {
        "canvasWidth": 60,
        "canvasHeight": 40,
        "layers": [
            {"id": "base", "order": 1, "blendMode": "normal", "opacity": 100,
             "x": 0, "y": 0},
            {"id": "mult", "order": 2, "blendMode": "multiply", "opacity": 100,
             "x": 0, "y": 0},
            {"id": "unknown", "order": 3, "blendMode": "screen", "opacity": 100,
             "x": 20, "y": 0},
        ],
    }
    _write_sample("blend", manifest, {"base": base, "mult": mult, "unknown": unknown})


# ---------------------------------------------------------------------------
# 3. opacity clamping
# ---------------------------------------------------------------------------


def sample_opacity() -> None:
    manifest = {
        "canvasWidth": 30,
        "canvasHeight": 30,
        "layers": [
            {"id": "under", "order": 1, "blendMode": "normal", "opacity": 100},
            {"id": "high", "order": 2, "blendMode": "normal", "opacity": 150},
            {"id": "low", "order": 3, "blendMode": "normal", "opacity": -10},
        ],
    }
    layers = {
        "under": _solid(30, 30, (200, 30, 30, 255)),
        "high": _solid(30, 30, (30, 180, 60, 255)),
        "low": _solid(30, 30, (20, 20, 200, 255)),
    }
    _write_sample("opacity", manifest, layers)


# ---------------------------------------------------------------------------
# 4. canvas clipping
# ---------------------------------------------------------------------------


def sample_clip() -> None:
    big = _noisy_alpha_blob(250, 250, salt=41, color=(120, 200, 90), margin=2)
    manifest = {
        "canvasWidth": 200,
        "canvasHeight": 200,
        "layers": [
            {"id": "big", "order": 1, "blendMode": "normal", "opacity": 100,
             "align": "center"},
        ],
    }
    _write_sample("clip", manifest, {"big": big})


# ---------------------------------------------------------------------------
# 5/6. crops + per-layer metrics
# ---------------------------------------------------------------------------


def sample_metrics() -> None:
    opaque = _noisy_alpha_blob(20, 20, salt=7, color=(210, 80, 120))
    empty = Image.new("RGBA", (12, 12), (255, 255, 255, 0))
    partial = _new_layer(16, 16)
    for index, color in enumerate([(0, 0, 0, 0), (0, 160, 220, 255),
                                    (0, 0, 0, 0), (0, 160, 220, 255)]):
        partial.paste(Image.new("RGBA", (8, 8), color), ((index % 2) * 8,
                                                         (index // 2) * 8))
    corner = _solid(10, 10, (255, 255, 255, 200))
    manifest = {
        "canvasWidth": 64,
        "canvasHeight": 64,
        "layers": [
            {"id": "opaque", "order": 1, "blendMode": "normal", "opacity": 100,
             "x": 5, "y": 5},
            {"id": "empty", "order": 2, "blendMode": "normal", "opacity": 100,
             "x": 40, "y": 40},
            {"id": "partial", "order": 3, "blendMode": "normal", "opacity": 100,
             "x": 30, "y": 0},
            {"id": "corner", "order": 4, "blendMode": "normal", "opacity": 100,
             "x": 60, "y": 60},
        ],
    }
    _write_sample("metrics", manifest,
                  {"opaque": opaque, "empty": empty, "partial": partial,
                   "corner": corner})


# ---------------------------------------------------------------------------
# 7. missing / undecodable layers
# ---------------------------------------------------------------------------


def sample_missing() -> None:
    layers = {
        "one": _solid(24, 24, (240, 80, 40, 255)),
        "three": _solid(24, 24, (40, 80, 240, 255)),
    }
    manifest = {
        "canvasWidth": 32,
        "canvasHeight": 32,
        "layers": [
            {"id": "one", "order": 1, "blendMode": "normal", "opacity": 100,
             "x": 0, "y": 0},
            {"id": "two", "order": 2, "blendMode": "normal", "opacity": 100,
             "x": 4, "y": 4},
            {"id": "three", "order": 3, "blendMode": "normal", "opacity": 100,
             "x": 8, "y": 8},
        ],
    }
    _write_sample("missing", manifest, layers)


def sample_corrupt() -> None:
    layers = {
        "good": _solid(16, 16, (30, 160, 90, 255)),
        "fine": _solid(8, 8, (220, 120, 30, 200)),
    }
    manifest = {
        "canvasWidth": 24,
        "canvasHeight": 24,
        "layers": [
            {"id": "good", "order": 1, "blendMode": "normal", "opacity": 100,
             "x": 0, "y": 0},
            {"id": "broken", "order": 2, "blendMode": "multiply", "opacity": 100,
             "x": 4, "y": 4},
            {"id": "fine", "order": 3, "blendMode": "normal", "opacity": 100,
             "x": 8, "y": 8},
        ],
    }
    _write_sample("corrupt", manifest, layers)
    # Deterministic bytes that Pillow cannot decode as a PNG.
    broken_path = os.path.join(ROOT, "corrupt", "layers", "broken.png")
    with open(broken_path, "wb") as handle:
        handle.write(b"NOT-A-PNG\n" + bytes(range(16)) * 4)


# ---------------------------------------------------------------------------
# 8/9. duplicate ids
# ---------------------------------------------------------------------------


def sample_duplicate() -> None:
    layers = {
        "dup": _solid(16, 16, (200, 50, 50, 255)),
        "solo": _solid(16, 16, (50, 200, 50, 255)),
    }
    manifest = {
        "canvasWidth": 20,
        "canvasHeight": 20,
        "layers": [
            {"id": "dup", "order": 2, "blendMode": "normal", "opacity": 100,
             "x": 4, "y": 4},
            {"id": "dup", "order": 1, "blendMode": "normal", "opacity": 100,
             "x": 0, "y": 0},
            {"id": "solo", "order": 3, "blendMode": "normal", "opacity": 100,
             "x": 2, "y": 2},
        ],
    }
    _write_sample("duplicate", manifest, layers)


SAMPLES = {
    "order": sample_order,
    "blend": sample_blend,
    "opacity": sample_opacity,
    "clip": sample_clip,
    "metrics": sample_metrics,
    "missing": sample_missing,
    "corrupt": sample_corrupt,
    "duplicate": sample_duplicate,
}


def main(argv: list[str] | None = None) -> None:
    global ROOT, _seed_offset
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=HERE,
                        help="directory to write samples into (default: samples/)")
    parser.add_argument("--seed", type=int, default=SEED,
                        help="optional seed override for exploratory runs")
    args = parser.parse_args(argv)
    ROOT = os.path.abspath(args.root)
    _seed_offset = args.seed - SEED
    os.makedirs(ROOT, exist_ok=True)
    for name, builder in SAMPLES.items():
        builder()


if __name__ == "__main__":
    main()
