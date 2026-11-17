"""Deterministic sample generation and the multimodal loading layer.

A sample directory contains ``image.png`` (fixed-resolution RGB image made
of geometric primitives plus seeded noise) and ``assertions.json`` (entities,
attributes and assertions).  Generation is fully deterministic: the same
sample name, index and seed always produce byte-identical files.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

from .errors import E_SAMPLE_INVALID, PipelineError, STAGE_LOAD

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_PRIMITIVES = ("rect", "ellipse", "line")


# ---------------------------------------------------------------------------
# Deterministic generation
# ---------------------------------------------------------------------------

def _stable_seed(*parts: Any) -> int:
    """Combine integers deterministically into a 63-bit NumPy seed."""
    value = 0
    for part in parts:
        value = (value * 1_000_003) ^ (int(part) + 0x9E3779B9)
    return value & 0x7FFFFFFF


def generate_image(sample_index: int, width: int, height: int,
                   seed: int) -> bytes:
    """Render a deterministic geometric PNG and return its raw bytes."""
    rng = np.random.default_rng(_stable_seed(seed, sample_index, 7))
    base = rng.integers(0, 256, size=3, dtype=np.uint8)
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    canvas[:, :, :] = base

    for primitive_index in range(3):
        sub = np.random.default_rng(
            _stable_seed(seed, sample_index, primitive_index, 11))
        kind = _PRIMITIVES[int(sub.integers(0, len(_PRIMITIVES)))]
        color = sub.integers(0, 256, size=3).astype(np.uint8)
        x0 = int(sub.integers(0, max(width // 2, 1)))
        y0 = int(sub.integers(0, max(height // 2, 1)))
        x1 = int(sub.integers(width // 2, width))
        y1 = int(sub.integers(height // 2, height))
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        if kind == "rect":
            canvas[y0:y1, x0:x1, :] = color
        elif kind == "ellipse":
            yy, xx = np.ogrid[:height, :width]
            mask = ((xx - (x0 + x1) / 2) / max((x1 - x0) / 2, 1)) ** 2 + (
                (yy - (y0 + y1) / 2) / max((y1 - y0) / 2, 1)) ** 2 <= 1
            canvas[mask] = color
        else:  # line
            steps = max(abs(x1 - x0), abs(y1 - y0), 1)
            xs = np.linspace(x0, x1, steps).astype(int)
            ys = np.linspace(y0, y1, steps).astype(int)
            canvas[np.clip(ys, 0, height - 1),
                   np.clip(xs, 0, width - 1)] = color

    noise_rng = np.random.default_rng(_stable_seed(seed, sample_index, 97))
    noise = noise_rng.integers(0, 32, size=(height, width, 3), dtype=np.int16)
    canvas = np.clip(canvas.astype(np.int16) + noise - 16, 0, 255)
    canvas = canvas.astype(np.uint8)

    buffer = io.BytesIO()
    with Image.fromarray(canvas, mode="RGB") as image:
        image.save(buffer, format="PNG", compress_level=6)
    return buffer.getvalue()


def generate_assertions(sample_name: str, sample_index: int,
                        region_count: int, seed: int) -> dict:
    """Build the structured assertion document for a sample."""
    rng = np.random.default_rng(_stable_seed(seed, sample_index, 131))
    attributes = ("brightness", "symmetry", "texture", "shape", "color")
    assertions = []
    for assertion_index in range(region_count + 1):
        attribute = attributes[int(rng.integers(0, len(attributes)))]
        region = int(rng.integers(0, region_count))
        value = round(float(rng.uniform(0.2, 0.9)), 4)
        assertions.append({
            "assertion_id": f"{sample_name}-a{assertion_index}",
            "entity": f"entity-{int(rng.integers(0, 3))}",
            "attribute": attribute,
            "region_ref": f"region-{region}",
            "value": value,
        })
    return {
        "sample": sample_name,
        "entities": [
            {"entity_id": "entity-0", "label": "foreground"},
            {"entity_id": "entity-1", "label": "background"},
            {"entity_id": "entity-2", "label": "structure"},
        ],
        "attributes": list(attributes),
        "assertions": assertions,
    }


def generate_sample_tree(output_dir: str, sample_count: int,
                         width: int, height: int, seed: int,
                         grid_rows: int, grid_cols: int) -> list[str]:
    """Create ``sample_count`` deterministic sample directories."""
    names: list[str] = []
    for index in range(sample_count):
        name = f"sample-{index:03d}"
        sample_dir = os.path.join(output_dir, name)
        os.makedirs(sample_dir, exist_ok=True)
        png_bytes = generate_image(index, width, height, seed)
        with open(os.path.join(sample_dir, "image.png"), "wb") as handle:
            handle.write(png_bytes)
        document = generate_assertions(name, index, grid_rows * grid_cols, seed)
        with open(os.path.join(sample_dir, "assertions.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
        names.append(name)
    return names


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoadedSample:
    name: str
    directory: str
    png_bytes: bytes
    image: np.ndarray
    assertions: dict


def _decode_png(png_bytes: bytes, expected_width: int,
                expected_height: int) -> np.ndarray:
    if not png_bytes.startswith(_PNG_MAGIC):
        raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                            "PNG container header missing or corrupted")
    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            image.verify()
    except Exception as exc:  # Pillow raises a variety of error types.
        raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                            f"PNG verification failed: {exc}") from exc
    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            if image.format != "PNG":
                raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                                    "file is not a PNG container")
            if image.size != (expected_width, expected_height):
                raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                                    f"size {image.size} != "
                                    f"({expected_width}, {expected_height})")
            array = np.asarray(image.convert("RGB"), dtype=np.uint8)
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                            f"PNG decode failed: {exc}") from exc
    if array.shape != (expected_height, expected_width, 3):
        raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                            f"decoded shape {array.shape} is unexpected")
    return array


def _validate_assertions(document: Any) -> dict:
    if not isinstance(document, dict):
        raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                            "assertion root must be an object")
    for key in ("sample", "entities", "attributes", "assertions"):
        if key not in document:
            raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                                f"assertion document missing field: {key}")
    if not isinstance(document["entities"], list) or not document["entities"]:
        raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                            "entities must be a non-empty list")
    for entity in document["entities"]:
        if not isinstance(entity, dict) or "entity_id" not in entity:
            raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                                "each entity needs an entity_id")
    if not isinstance(document["attributes"], list) \
            or not all(isinstance(item, str) for item in document["attributes"]):
        raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                            "attributes must be a list of strings")
    assertions = document["assertions"]
    if not isinstance(assertions, list) or not assertions:
        raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                            "assertions must be a non-empty list")
    seen = set()
    for assertion in assertions:
        if not isinstance(assertion, dict):
            raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                                "each assertion must be an object")
        for field in ("assertion_id", "entity", "attribute",
                      "region_ref", "value"):
            if field not in assertion:
                raise PipelineError(
                    E_SAMPLE_INVALID, STAGE_LOAD,
                    f"assertion missing field: {field}")
        if assertion["assertion_id"] in seen:
            raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                                "duplicate assertion_id: "
                                f"{assertion['assertion_id']}")
        seen.add(assertion["assertion_id"])
        if not isinstance(assertion["value"], (int, float)) \
                or isinstance(assertion["value"], bool):
            raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                                "assertion value must be numeric")
    return document


def load_sample(sample_dir: str, expected_width: int,
                expected_height: int) -> LoadedSample:
    """Validate and load one sample, raising :class:`PipelineError`."""
    name = os.path.basename(os.path.normpath(sample_dir))
    image_path = os.path.join(sample_dir, "image.png")
    assertion_path = os.path.join(sample_dir, "assertions.json")
    if not os.path.isfile(image_path):
        raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                            f"missing image file: {image_path}")
    if not os.path.isfile(assertion_path):
        raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                            f"missing assertion file: {assertion_path}")

    with open(image_path, "rb") as handle:
        png_bytes = handle.read()
    image = _decode_png(png_bytes, expected_width, expected_height)

    try:
        with open(assertion_path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except json.JSONDecodeError as exc:
        raise PipelineError(E_SAMPLE_INVALID, STAGE_LOAD,
                            f"assertion JSON malformed: {exc}") from exc
    document = _validate_assertions(document)
    return LoadedSample(name=name, directory=sample_dir, png_bytes=png_bytes,
                        image=image, assertions=document)


def discover_sample_dirs(input_dir: str) -> list[str]:
    """Return sample directories sorted by sample name for stable ordering."""
    if not os.path.isdir(input_dir):
        raise FileNotFoundError(input_dir)
    entries = [
        os.path.join(input_dir, name)
        for name in os.listdir(input_dir)
        if os.path.isdir(os.path.join(input_dir, name))
    ]
    return sorted(entries, key=lambda path: os.path.basename(path))
