"""Acceptance test helpers shared by the per-requirement test modules."""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def copy_sample(tmp_path: Path, name: str) -> Path:
    """Copy a generated sample into a per-test directory, return spec path."""
    source = ROOT / "samples" / name
    target = tmp_path / name
    shutil.copytree(source, target)
    return target / "description.json"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_image(path: Path) -> Image.Image:
    image = Image.open(path)
    image.load()
    return image


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def write_description(directory: Path, frames: list[dict], **overrides) -> Path:
    payload = {
        "canvas": {"width": 320, "height": 180},
        "background": "#101820",
        "fps": 24,
        "frames": frames,
    }
    payload.update(overrides)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "description.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
