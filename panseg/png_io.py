"""Deterministic PNG panoptic annotation image input/output.

Encoding contract for the fixed-resolution annotation PNGs:

* red channel   - local instance index (unique within a scene)
* green channel - semantic class numeric id
* blue channel  - zero

The encoding is reversible for the generated sample set and keeps every image
a compact mode ``RGB`` PNG written by Pillow.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from .rle import runs_to_bitmap


def write_panoptic_png(path: str | Path, label_map: np.ndarray, class_lookup: dict[str, int]) -> None:
    height, width = label_map.shape
    rgb = np.zeros((height, width, 3), dtype=np.uint8)
    rgb[:, :, 0] = label_map.astype(np.uint8)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb, mode="RGB").save(path, format="PNG", optimize=False)


def write_instance_png(
    path: str | Path,
    runs,
    height: int,
    width: int,
    instance_index: int,
    class_index: int,
) -> None:
    bitmap = runs_to_bitmap(runs, height, width)
    rgb = np.zeros((height, width, 3), dtype=np.uint8)
    rgb[:, :, 0] = bitmap * instance_index
    rgb[:, :, 1] = bitmap * class_index
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb, mode="RGB").save(path, format="PNG", optimize=False)


def read_panoptic_png(path: str | Path) -> np.ndarray:
    image = Image.open(path)
    array = np.asarray(image.convert("RGB"), dtype=np.uint8)
    return array
