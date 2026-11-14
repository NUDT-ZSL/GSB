"""Deterministic sample sources used by the demo and by manual checks.

Everything here is computed from pixel coordinates or written as fixed
bytes, so repeated runs produce byte-identical source PNGs.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from PIL import Image, ImageDraw

SAMPLE_DIR = Path(__file__).resolve().parent


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return (
        struct.pack(">I", len(payload))
        + body
        + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    )


def build_world_map(width: int = 500, height: int = 300) -> Image.Image:
    """A 500x300 RGBA map with quadrant tints, terrain bands and a lake.

    The four quadrants have visibly different base tints so a tile that
    accidentally samples a neighbouring region is easy to spot.
    """
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pixels = image.load()

    quadrant_tint = (
        (40, 90, 60),    # top-left: forest
        (120, 100, 50),  # top-right: desert
        (40, 70, 110),   # bottom-left: sea
        (90, 60, 90),    # bottom-right: highlands
    )
    for y in range(height):
        for x in range(width):
            qx = 0 if x < width // 2 else 1
            qy = 0 if y < height // 2 else 1
            tint = quadrant_tint[qy * 2 + qx]
            # Smooth, deterministic terrain variation.
            wave = ((x * 13 + y * 7) % 64) - 32
            pixels[x, y] = (
                max(0, min(255, tint[0] + wave)),
                max(0, min(255, tint[1] + wave)),
                max(0, min(255, tint[2] + wave)),
                255,
            )

    draw = ImageDraw.Draw(image)
    # A river crossing tile boundaries, so seams are visible if tiles are
    # cropped with an off-by-one offset.
    draw.line([(0, 210), (130, 190), (270, 205), (499, 170)],
              fill=(30, 140, 200, 255), width=9)
    # Opaque landmark near the bottom-right of the image.
    draw.ellipse([(400, 230), (470, 295)], fill=(235, 210, 90, 255))
    # Semi-transparent fog patch.
    draw.ellipse([(300, 20), (380, 80)], fill=(255, 255, 255, 90))
    # Fully transparent hole (alpha 0) fully inside one 256 px tile.
    draw.rectangle([(20, 20), (90, 90)], fill=(0, 0, 0, 0))
    return image


def build_transparent_half(width: int = 512, height: int = 256) -> Image.Image:
    """Left 256 columns fully transparent, right half filled with stripes."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pixels = image.load()
    for y in range(height):
        for x in range(256, width):
            pixels[x, y] = (x % 256, (y * 3) % 256, 200, 255)
    return image


def write_corrupt_png(path: Path) -> None:
    """Write a structurally valid PNG whose pixel data is destroyed."""
    width, height = 64, 64
    image = build_world_map(width, height)
    row_bytes = width * 4
    flat = image.tobytes()
    raw = b"".join(
        b"\x00" + flat[y * row_bytes : (y + 1) * row_bytes]
        for y in range(height)
    )
    png = bytearray(b"\x89PNG\r\n\x1a\n")
    png += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(raw))
    png += _chunk(b"IEND", b"")

    # Keep the signature and IHDR, trash the compressed data and every
    # trailing CRC. A decoder must reject this file.
    png[70:] = b"\x00" * (len(png) - 70)
    path.write_bytes(bytes(png))


def main() -> None:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    build_world_map().save(SAMPLE_DIR / "world_500x300.png")
    build_transparent_half().save(SAMPLE_DIR / "transparent_half_512x256.png")
    write_corrupt_png(SAMPLE_DIR / "corrupt.png")
    # An image Pillow can open, but in a format the tiler refuses.
    build_world_map(128, 128).save(SAMPLE_DIR / "unsupported.tga")
    for name in (
        "world_500x300.png",
        "transparent_half_512x256.png",
        "corrupt.png",
        "unsupported.tga",
    ):
        path = SAMPLE_DIR / name
        print(f"{name}: {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
