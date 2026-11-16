"""Deterministic sample generation and validated multimodal sample loading."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .png_codec import PngError, decode_png, encode_png

IMAGE_WIDTH = 64
IMAGE_HEIGHT = 64
E_SAMPLE_INVALID = "E_SAMPLE_INVALID"

ATTRIBUTES = ["color", "shape", "size", "orientation", "texture"]
SHAPES = ["circle", "rectangle", "triangle", "line"]
COLORS = ["red", "green", "blue", "yellow"]
ORIENTATIONS = ["horizontal", "vertical", "diagonal"]
TEXTURES = ["solid", "striped", "noisy"]
SIZES = ["small", "medium", "large"]
_PALETTE = {
    "red": (220, 40, 40),
    "green": (40, 200, 70),
    "blue": (40, 80, 220),
    "yellow": (230, 210, 50),
}


@dataclass
class Sample:
    name: str
    image: np.ndarray
    description: dict

    @property
    def entities(self) -> list[dict]:
        return list(self.description.get("entities", []))

    @property
    def assertions(self) -> list[dict]:
        return list(self.description.get("assertions", []))


def _rng_for(seed: int) -> np.random.Generator:
    return np.random.default_rng(np.random.MT19937(seed))


def _draw_shape(canvas: np.ndarray, rng: np.random.Generator, shape: str,
                color: tuple[int, int, int], grid_row: int, grid_col: int,
                cell_h: int, cell_w: int) -> None:
    y0, x0 = grid_row * cell_h, grid_col * cell_w
    cy, cx = y0 + cell_h // 2, x0 + cell_w // 2
    radius = min(cell_h, cell_w) // 3
    yy, xx = np.mgrid[0:canvas.shape[0], 0:canvas.shape[1]]
    if shape == "circle":
        mask = (yy - cy) ** 2 + (xx - cx) ** 2 <= radius ** 2
    elif shape == "rectangle":
        mask = ((np.abs(xx - cx) <= radius) & (np.abs(yy - cy) <= radius))
    elif shape == "triangle":
        mask = (yy >= cy - radius) & (yy <= cy + radius) & \
               (np.abs(xx - cx) <= (cy + radius - yy))
    else:
        band = max(1, cell_h // 8)
        mask = (np.abs(yy - cy) <= band) & (np.abs(xx - cx) <= radius)
    canvas[mask] = color


def _paint_cell(canvas: np.ndarray, grid_row: int, grid_col: int,
                shape: str, color: str, fill: int) -> None:
    rows, cols = 4, 4
    cell_h, cell_w = IMAGE_HEIGHT // rows, IMAGE_WIDTH // cols
    cell = canvas[grid_row * cell_h:(grid_row + 1) * cell_h,
                  grid_col * cell_w:(grid_col + 1) * cell_w]
    cell[:] = fill
    _draw_shape(canvas, _rng_for(0), shape, _PALETTE[color], grid_row,
                grid_col, cell_h, cell_w)


def _generate_image(seed: int, profile: str) -> np.ndarray:
    rng = _rng_for(seed)
    canvas = np.full((IMAGE_HEIGHT, IMAGE_WIDTH, 3), 245, dtype=np.uint8)

    if profile == "mismatch":
        # Text claims a red circle, but the referenced regions show blue
        # triangles: deterministic incompatible evidence.
        for r, c in [(0, 0), (0, 1), (1, 0), (1, 1), (2, 2), (3, 3)]:
            _paint_cell(canvas, r, c, "triangle", "blue", 235)
    else:
        spec = {
            "fast": ("circle", "red"),
            "slow": ("rectangle", "green"),
            "conflict": ("triangle", "blue"),
            "dangling": ("line", "yellow"),
        }[profile]
        shape, color = spec
        for r in range(4):
            for c in range(4):
                if r == 0 and c == 0:
                    _paint_cell(canvas, r, c, shape, color, 250)
                elif (r, c) in {(1, 1), (2, 2), (3, 3)} or                         (r == 0 and c in (1, 2, 3)):
                    _paint_cell(canvas, r, c, shape, color, 248)
                else:
                    random_shape = SHAPES[int(rng.integers(len(SHAPES)))]
                    random_color = COLORS[int(rng.integers(len(COLORS)))]
                    _paint_cell(canvas, r, c, random_shape, random_color, 245)

    noise = rng.integers(0, 4, size=canvas.shape, dtype=np.int16)
    canvas = np.clip(canvas.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    return canvas


def _build_description(profile: str) -> dict:
    if profile == "fast":
        color, shape = "red", "circle"
        conflict = False
        supplements = 1
    elif profile == "slow":
        color, shape = "green", "rectangle"
        conflict = False
        supplements = 3
    elif profile == "conflict":
        color, shape = "blue", "triangle"
        conflict = True
        supplements = 1
    elif profile == "dangling":
        color, shape = "red", "line"
        conflict = False
        supplements = 1
    else:  # mismatch
        color, shape = "red", "circle"
        conflict = False
        supplements = 0

    assertions = [
        {"assertion_id": "a-color", "entity": "e1", "attribute": "color",
         "value": color, "confidence": 0.9},
        {"assertion_id": "a-shape", "entity": "e1", "attribute": "shape",
         "value": shape, "confidence": 0.85},
        {"assertion_id": "a-size", "entity": "e1", "attribute": "size",
         "value": "medium", "confidence": 0.8},
        {"assertion_id": "a-orientation", "entity": "e1", "attribute":
            "orientation", "value": "horizontal", "confidence": 0.75},
        {"assertion_id": "a-texture", "entity": "e1", "attribute": "texture",
         "value": "solid", "confidence": 0.7},
    ]
    description = {
        "sample_profile": profile,
        "entities": [{"entity_id": "e1", "name": "primary_figure"}],
        "attributes": [{"attribute": name} for name in ATTRIBUTES],
        "assertions": assertions,
        "expected_primary_region": "r0c0",
        "conflict": conflict,
        "supplements": supplements,
    }
    return description


def _build_relations(profile: str) -> list[dict]:
    rels = [
        {"relation_id": "rel-color", "type": "associate",
         "assertion_id": "a-color", "region_id": "r0c0", "weight": 1.0},
        {"relation_id": "rel-shape", "type": "associate",
         "assertion_id": "a-shape", "region_id": "r0c0", "weight": 0.9},
        {"relation_id": "rel-size", "type": "associate",
         "assertion_id": "a-size", "region_id": "r1c1", "weight": 0.7},
        {"relation_id": "rel-orientation", "type": "associate",
         "assertion_id": "a-orientation", "region_id": "r2c2",
         "weight": 0.6},
        {"relation_id": "rel-texture", "type": "associate",
         "assertion_id": "a-texture", "region_id": "r3c3", "weight": 0.5},
    ]
    if profile == "conflict":
        rels.append({"relation_id": "rel-conflict", "type": "conflict",
                     "assertion_id": "a-shape", "region_id": "r0c0",
                     "weight": 0.8})
    for index in range(1, {"fast": 2, "slow": 4, "conflict": 2,
                           "dangling": 2, "mismatch": 1}[profile]):
        rels.append({"relation_id": f"rel-supp-{index}", "type":
                     "supplement", "assertion_id": "a-color",
                     "region_id": f"r0c{index}", "weight": 0.6})
    if profile == "dangling":
        rels.append({"relation_id": "rel-dangling-region", "type":
                     "associate", "assertion_id": "a-color",
                     "region_id": "r9c9", "weight": 1.0})
        rels.append({"relation_id": "rel-dangling-assertion", "type":
                     "supplement", "assertion_id": "a-missing",
                     "region_id": "r0c0", "weight": 1.0})
    return rels


def generate_dataset(input_dir: str | Path, seed: int = 20261117) -> dict:
    """Generate deterministic sample packages, relation table and config."""
    input_dir = Path(input_dir)
    input_dir.mkdir(parents=True, exist_ok=True)
    profiles = ["fast", "slow", "conflict", "dangling", "mismatch"]

    for index, profile in enumerate(profiles):
        sample_dir = input_dir / f"sample_{profile}"
        sample_dir.mkdir(parents=True, exist_ok=True)
        image = _generate_image(seed + index, profile)
        (sample_dir / "image.png").write_bytes(encode_png(image))
        description = _build_description(profile)
        (sample_dir / "description.json").write_text(
            json.dumps(description, ensure_ascii=False, indent=2),
            encoding="utf-8")
        relations = _build_relations(profile)
        (sample_dir / "relations.json").write_text(
            json.dumps(relations, ensure_ascii=False, indent=2),
            encoding="utf-8")

    # Invalid samples exercise the resilient loading layer.
    bad_dir = input_dir / "sample_bad_missing"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "description.json").write_text(
        json.dumps({"entities": [], "attributes": [], "assertions": []}),
        encoding="utf-8")

    bad_png = input_dir / "sample_bad_header"
    bad_png.mkdir(parents=True, exist_ok=True)
    (bad_png / "image.png").write_bytes(b"NOT_A_PNG_FILE")
    (bad_png / "description.json").write_text(
        json.dumps({"entities": [], "attributes": [], "assertions": []}),
        encoding="utf-8")

    bad_json = input_dir / "sample_bad_json"
    bad_json.mkdir(parents=True, exist_ok=True)
    (bad_json / "image.png").write_bytes(encode_png(
        np.full((IMAGE_HEIGHT, IMAGE_WIDTH, 3), 100, dtype=np.uint8)))
    (bad_json / "description.json").write_text("{not valid json",
                                               encoding="utf-8")

    config = {
        "feature_dim": 12,
        "quantization_levels": 8,
        "text_evidence_weight": 0.55,
        "fusion_temperature": 1.0,
        "confidence_threshold": 0.70,
        "max_evidence_rounds": 4,
        "concurrency": 3,
        "grid_rows": 4,
        "grid_cols": 4,
        "conflict_penalty": 0.25,
    }
    (input_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"input_dir": str(input_dir), "profiles": profiles,
            "config": config}


def _validate_description(data) -> dict:
    if not isinstance(data, dict):
        raise ValueError("description root must be an object")
    for key in ("entities", "attributes", "assertions"):
        if key not in data or not isinstance(data[key], list):
            raise ValueError(f"field {key} must be a list")
    seen: set[str] = set()
    for assertion in data["assertions"]:
        if not isinstance(assertion, dict):
            raise ValueError("each assertion must be an object")
        for key in ("assertion_id", "attribute", "value"):
            if key not in assertion:
                raise ValueError(f"assertion missing key {key}")
        aid = str(assertion["assertion_id"])
        if aid in seen:
            raise ValueError(f"duplicate assertion_id {aid}")
        seen.add(aid)
        if str(assertion["attribute"]) not in ATTRIBUTES:
            raise ValueError(
                f"unsupported attribute {assertion['attribute']}")
    return data


def load_sample(sample_dir: Path) -> Sample:
    image_path = sample_dir / "image.png"
    desc_path = sample_dir / "description.json"
    if not image_path.is_file():
        raise FileNotFoundError("image.png missing")
    if not desc_path.is_file():
        raise FileNotFoundError("description.json missing")

    raw_png = image_path.read_bytes()
    if raw_png[:8] != b"\x89PNG\r\n\x1a\n":
        raise PngError("invalid PNG container header")
    image = decode_png(raw_png)
    if image.dtype != np.uint8 or image.ndim not in (2, 3):
        raise PngError("decoded image must be a uint8 array")

    try:
        description = json.loads(desc_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"description JSON invalid: {exc}") from exc
    description = _validate_description(description)
    return Sample(name=sample_dir.name, image=image,
                  description=description)


def discover_samples(input_dir: str | Path):
    """Return (valid samples, failures) preserving directory sort order."""
    input_dir = Path(input_dir)
    samples: list[Sample] = []
    failures: list[dict] = []
    for sample_dir in sorted(p for p in input_dir.iterdir() if p.is_dir()):
        try:
            samples.append(load_sample(sample_dir))
        except (PngError, ValueError, OSError) as exc:
            stage = "sample_loading"
            if isinstance(exc, FileNotFoundError):
                stage = "file_existence"
            elif isinstance(exc, PngError):
                stage = "png_validation"
            elif isinstance(exc, ValueError):
                stage = "json_validation"
            failures.append({
                "sample_name": sample_dir.name,
                "round": None,
                "stage": stage,
                "code": E_SAMPLE_INVALID,
                "exception_type": type(exc).__name__,
                "message": str(exc),
            })
    return samples, failures
