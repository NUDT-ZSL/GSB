"""Multimodal sample loading and validation (requirement 1)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from . import E_SAMPLE_INVALID

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

CONFIG_DEFAULTS = {
    "image_size": 64,
    "feature_dim": 8,
    "quant_levels": 32,
    "text_weight": 2.5,
    "fusion_temperature": 1.0,
    "confidence_threshold": 0.38,
    "frac_bits": 12,
    "concurrency": 4,
    "rounds": 3,
    "error_tolerance": 2,
    "boundary_margin": 0.02,
    "conflict_reduction": 0.5,
    "supplement_bonus": 0.25,
    "classes": ["supported", "refuted", "uncertain"],
}


@dataclass
class Sample:
    name: str
    image: np.ndarray
    entities: list
    attributes: list
    assertions: list


def load_config(path: Path) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg = dict(CONFIG_DEFAULTS)
    cfg.update(raw)
    if cfg["feature_dim"] < 1:
        raise ValueError("feature_dim must be >= 1")
    if cfg["frac_bits"] < 4:
        raise ValueError("frac_bits must be >= 4")
    return cfg


def load_relations(path: Path) -> list:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rels = raw.get("relations", []) if isinstance(raw, dict) else raw
    if not isinstance(rels, list):
        raise ValueError("relations table must be a list")
    return rels


def _validate_png(path: Path, image_size: int) -> np.ndarray:
    head = path.read_bytes()[:8]
    if head != PNG_SIGNATURE:
        raise ValueError("bad PNG container header")
    with Image.open(path) as im:
        if im.format != "PNG":
            raise ValueError("not a PNG image")
        if im.size != (image_size, image_size):
            raise ValueError(
                f"image size {im.size} != expected {(image_size, image_size)}"
            )
        arr = np.asarray(im.convert("L"), dtype=np.uint8)
    return arr


def _validate_assertions(path: Path, classes: list) -> tuple[list, list, list]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"assertions JSON not parseable: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError("assertions JSON root must be an object")
    entities = doc.get("entities")
    attributes = doc.get("attributes")
    assertions = doc.get("assertions")
    if not isinstance(entities, list) or not isinstance(attributes, list):
        raise ValueError("entities/attributes must be lists")
    if not isinstance(assertions, list) or not assertions:
        raise ValueError("assertions must be a non-empty list")
    for ent in entities:
        if not isinstance(ent, dict) or "id" not in ent:
            raise ValueError("entity missing id")
    for att in attributes:
        if not isinstance(att, dict) or "entity" not in att or "name" not in att:
            raise ValueError("attribute missing entity/name")
    for a in assertions:
        if not isinstance(a, dict) or "id" not in a:
            raise ValueError("assertion missing id")
        logits = a.get("class_logits")
        if not isinstance(logits, dict):
            raise ValueError(f"assertion {a.get('id')} missing class_logits")
        for c in classes:
            if not isinstance(logits.get(c), (int, float)):
                raise ValueError(
                    f"assertion {a.get('id')} missing numeric logit for {c}"
                )
    return entities, attributes, assertions


def load_samples(sample_dir: Path, cfg: dict):
    """Scan sample directories; invalid ones go to the failure list."""
    samples: list[Sample] = []
    failures: list[dict] = []
    sample_dir = Path(sample_dir)
    for child in sorted(p for p in sample_dir.iterdir() if p.is_dir()):
        name = child.name
        try:
            img_path = child / "image.png"
            json_path = child / "assertions.json"
            if not img_path.is_file():
                raise ValueError("missing image.png")
            if not json_path.is_file():
                raise ValueError("missing assertions.json")
            image = _validate_png(img_path, cfg["image_size"])
            entities, attributes, assertions = _validate_assertions(
                json_path, cfg["classes"]
            )
        except ValueError as exc:
            failures.append(
                {
                    "sample": name,
                    "round": None,
                    "stage": "load",
                    "code": E_SAMPLE_INVALID,
                    "detail": str(exc),
                }
            )
            continue
        samples.append(
            Sample(
                name=name,
                image=image,
                entities=entities,
                attributes=attributes,
                assertions=assertions,
            )
        )
    return samples, failures
