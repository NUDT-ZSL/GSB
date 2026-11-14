"""Deterministic sample dataset.

Everything here is fixed: no randomness, no timestamps, no external assets.
Running :func:`build_sample_dataset` twice produces byte-identical inputs.

Scene A - the main scene, deliberately contains every seam category:

* one light leak across a strong vertical brightness step (grey 50 -> 210),
* two clean seams inside uniform regions,
* one out-of-bounds seam (``u = 1.2``),
* one degenerate seam (both endpoints identical),
* one row that duplicates the leak seam with its endpoints reversed.

Scene B - clean, uniform: no leaks, no out-of-bounds seams.
Scene C - boundary scene: the brightness step is exactly the configured
threshold (grey 100 -> 132, difference 32.0 at threshold 32).
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

SAMPLE_SIZE = 512
SAMPLE_THRESHOLD = 32
SETTINGS_FILENAME = "bake_settings.json"


def _flat_split(size: int, left_grey: int, right_grey: int) -> Image.Image:
    """Left/right flat halves split exactly at the vertical centre."""

    image = Image.new("RGB", (size, size))
    pixels = image.load()
    half = size // 2
    for y in range(size):
        for x in range(size):
            grey = left_grey if x < half else right_grey
            pixels[x, y] = (grey, grey, grey)
    return image


def _uniform(size: int, grey: int) -> Image.Image:
    return Image.new("RGB", (size, size), (grey, grey, grey))


def scene_a(size: int = SAMPLE_SIZE) -> Image.Image:
    return _flat_split(size, 50, 210)


def scene_b(size: int = SAMPLE_SIZE) -> Image.Image:
    return _uniform(size, 128)


def scene_c(size: int = SAMPLE_SIZE) -> Image.Image:
    return _flat_split(size, 100, 132)


SCENE_A_SEAMS = [
    # seam_id, u1, v1, u2, v2
    ("leak_center", 0.5, 0.15, 0.5, 0.85),
    ("clean_left", 0.1, 0.3, 0.42, 0.3),
    ("clean_right", 0.6, 0.2, 0.9, 0.55),
    ("out_of_bounds", 1.2, 0.5, 0.8, 0.5),
    ("degenerate_point", 0.3, 0.7, 0.3, 0.7),
    # Duplicate of leak_center with endpoints reversed: counted once.
    ("leak_center_copy", 0.5, 0.85, 0.5, 0.15),
]

SCENE_B_SEAMS = [
    ("b_clean_1", 0.2, 0.2, 0.8, 0.2),
    ("b_clean_2", 0.2, 0.5, 0.2, 0.9),
    ("b_clean_3", 0.35, 0.6, 0.75, 0.95),
]

SCENE_C_SEAMS = [
    # Difference is exactly 32.0, equal to the threshold: not a leak.
    ("c_exact_threshold", 0.5, 0.1, 0.5, 0.9),
    ("c_clean", 0.2, 0.2, 0.4, 0.2),
]


def _write_csv(path: Path, rows: list[tuple]) -> None:
    lines = ["seam_id,u1,v1,u2,v2"]
    for row in rows:
        seam_id, u1, v1, u2, v2 = row
        lines.append(f"{seam_id},{u1:g},{v1:g},{u2:g},{v2:g}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_sample_dataset(destination: Path) -> Path:
    """Create the sample directory and return the settings file path."""

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    scene_a(SAMPLE_SIZE).save(destination / "sample_lightmap_a.png", format="PNG")
    scene_b(SAMPLE_SIZE).save(destination / "sample_lightmap_b.png", format="PNG")
    scene_c(SAMPLE_SIZE).save(destination / "sample_lightmap_c.png", format="PNG")

    _write_csv(destination / "sample_lightmap_a_seams.csv", SCENE_A_SEAMS)
    _write_csv(destination / "sample_lightmap_b_seams.csv", SCENE_B_SEAMS)
    _write_csv(destination / "sample_lightmap_c_seams.csv", SCENE_C_SEAMS)

    settings = {
        "version": 1,
        "threshold": SAMPLE_THRESHOLD,
        "lightmaps": [
            {
                "name": "sample_lightmap_a",
                "image": "sample_lightmap_a.png",
                "seams_csv": "sample_lightmap_a_seams.csv",
            },
            {
                "name": "sample_lightmap_b",
                "image": "sample_lightmap_b.png",
                "seams_csv": "sample_lightmap_b_seams.csv",
            },
            {
                "name": "sample_lightmap_c",
                "image": "sample_lightmap_c.png",
                "seams_csv": "sample_lightmap_c_seams.csv",
            },
        ],
    }
    settings_path = destination / SETTINGS_FILENAME
    settings_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return settings_path


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("samples")
    print(build_sample_dataset(target))
