"""Deterministically generate sample frame-sequence directories.

All pixels, sizes, modes and corrupt-byte sequences are derived from fixed
constants (no randomness, no timestamps), so rebuilding yields byte-identical
directories. No network access is used.

Generated scenarios under <out>/ (default: samples/):
  normal/          6 valid frames, indices 1..6, mixed sizes and modes
                   (RGB, RGBA, L, P, LA), some with real alpha
  with_corrupt/    normal set + frame_07.png containing non-PNG garbage
  all_corrupt/     3 indexed PNG names, all containing garbage bytes
  duplicate_index/ two files whose names both resolve to index 3
  missing_index/   indices 1, 2, 4 (gap at 3)

Usage:
    python generate_samples.py [--out samples] [--force]
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

# Fixed per-frame geometry (intentionally different), colors and modes.
SIZES = [(40, 32), (56, 44), (48, 36), (60, 48), (44, 30), (52, 40)]
MODES = ["RGB", "RGBA", "L", "RGBA", "P", "LA"]
BASE_COLORS = [
    (220, 60, 60),
    (60, 180, 90),
    (50, 90, 200),
    (200, 160, 40),
    (150, 70, 190),
    (50, 170, 180),
]
GARBAGE = b"THIS_IS_NOT_A_VALID_PNG_FILE" * 4


def draw_frame(slot: int, size: tuple[int, int], mode: str) -> Image.Image:
    """Build one deterministic frame; frames with an alpha channel also
    contain a semi-transparent band so alpha preservation can be checked."""
    width, height = size
    base = BASE_COLORS[slot]

    if mode in ("L", "LA"):
        gray = (base[0] * 299 + base[1] * 587 + base[2] * 114) // 1000
        image = Image.new(
            "RGBA" if mode == "LA" else "L",
            (width, height),
            (gray, gray, gray, 255) if mode == "LA" else gray,
        )
        band = Image.new("RGBA", (width, max(1, height // 3)), (0, 0, 0, 128))
        layer = image.convert("RGBA")
        layer.alpha_composite(band, (0, (height - band.height) // 2))
        return layer.convert("LA") if mode == "LA" else layer.convert("L")

    image = Image.new("RGBA", (width, height), base + (255,))
    draw = ImageDraw.Draw(image)
    # Fixed-geometry inner rectangle: deterministic and size-proportional.
    margin_x, margin_y = max(1, width // 8), max(1, height // 8)
    draw.rectangle(
        [margin_x, margin_y, width - 1 - margin_x, height - 1 - margin_y],
        fill=(255 - base[0], 255 - base[1], 255 - base[2], 255),
    )
    if mode == "RGBA":
        band = Image.new("RGBA", (width, max(1, height // 4)), (255, 255, 255, 128))
        image.alpha_composite(band, (0, (height - band.height) // 2))
        return image

    rgb = image.convert("RGB")
    if mode == "P":
        # Adaptive palette from a fixed image -> deterministic output.
        return rgb.convert("P", palette=Image.Palette.ADAPTIVE, colors=16)
    return rgb


def write_valid_frames(directory: Path, count: int = 6) -> None:
    for slot in range(count):
        image = draw_frame(slot, SIZES[slot], MODES[slot])
        image.save(directory / f"frame_{slot + 1:02d}.png", format="PNG")


def write_garbage(path: Path) -> None:
    path.write_bytes(GARBAGE)


def reset_dir(path: Path, force: bool) -> None:
    if path.exists():
        if not force:
            raise SystemExit(
                f"refusing to overwrite existing {path} (re-run with --force)"
            )
        shutil.rmtree(path)
    path.mkdir(parents=True)


def generate(out_root: Path, force: bool) -> None:
    scenarios = {
        "normal": lambda d: write_valid_frames(d),
        "with_corrupt": lambda d: (
            write_valid_frames(d),
            write_garbage(d / "frame_07.png"),
        ),
        "all_corrupt": lambda d: [
            write_garbage(d / f"frame_{i:02d}.png") for i in range(1, 4)
        ],
        "duplicate_index": lambda d: (
            write_valid_frames(d, count=3),
            (d / "frame_03_copy.png").write_bytes(
                (d / "frame_03.png").read_bytes()
            ),
        ),
        "missing_index": lambda d: (
            write_valid_frames(d, count=2),
            (d / "frame_04.png").write_bytes(
                (d / "frame_02.png").read_bytes()
            ),
        ),
    }
    for name, builder in scenarios.items():
        scenario_dir = out_root / name
        reset_dir(scenario_dir, force)
        builder(scenario_dir)
        pngs = sorted(p.name for p in scenario_dir.glob("*.png"))
        print(f"{name}: {len(pngs)} file(s) -> {scenario_dir}  {pngs}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="samples", help="output root directory")
    parser.add_argument(
        "--force", action="store_true", help="delete existing scenario directories"
    )
    args = parser.parse_args()
    generate(Path(args.out), args.force)


if __name__ == "__main__":
    main()
