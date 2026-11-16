"""Deterministic PNG image loading helpers (local files only)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from .errors import FormatInvalid


def load_rgb(path: str | Path) -> np.ndarray:
    path = Path(path)
    if not path.exists():
        raise FormatInvalid(f"图像文件不存在: {path}",
                            context={"path": str(path)})
    with Image.open(path) as img:
        rgb = img.convert("RGB")
        arr = np.asarray(rgb, dtype=np.uint8).copy()
    return arr


def save_png(path: str | Path, image: np.ndarray) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(image.astype(np.uint8), mode="RGB").save(
        path, format="PNG", optimize=False)
