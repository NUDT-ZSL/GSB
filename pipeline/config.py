"""Fusion configuration loading and validation."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass

from .hashing import canonical_json, content_hash

#: Fields accepted by :class:`FusionConfig` together with their validators.
_POSITIVE_INT = ("feature_dim", "quantization_levels", "max_rounds",
                 "concurrency", "grid_rows", "grid_cols",
                 "image_width", "image_height", "rng_seed")
_POSITIVE_FLOAT = ("text_weight", "conflict_discount", "supplement_boost",
                   "fusion_temperature", "confidence_threshold")


@dataclass(frozen=True)
class FusionConfig:
    """Immutable fusion configuration shared by every worker thread."""

    feature_dim: int
    quantization_levels: int
    text_weight: float
    fusion_temperature: float
    confidence_threshold: float
    max_rounds: int
    concurrency: int
    conflict_discount: float
    supplement_boost: float
    grid_rows: int
    grid_cols: int
    image_width: int
    image_height: int
    rng_seed: int

    def to_dict(self) -> dict:
        return asdict(self)

    def hashable_dict(self) -> dict:
        """Decision-affecting fields only; concurrency never changes results."""
        data = asdict(self)
        data.pop("concurrency")
        return data

    def canonical(self) -> str:
        return canonical_json(self.hashable_dict())

    def hash(self) -> str:
        return content_hash(self.hashable_dict())

    @property
    def region_count(self) -> int:
        return self.grid_rows * self.grid_cols


def load_fusion_config(path: str) -> FusionConfig:
    """Read, validate and normalize a fusion configuration JSON file."""
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("fusion config root must be a JSON object")

    defaults = {
        "feature_dim": 12,
        "quantization_levels": 16,
        "text_weight": 0.55,
        "fusion_temperature": 1.0,
        "confidence_threshold": 0.55,
        "max_rounds": 4,
        "concurrency": 3,
        "conflict_discount": 0.5,
        "supplement_boost": 0.25,
        "grid_rows": 2,
        "grid_cols": 2,
        "image_width": 64,
        "image_height": 64,
        "rng_seed": 20261117,
    }
    unknown = set(raw) - set(defaults)
    if unknown:
        raise ValueError(f"unknown fusion config keys: {sorted(unknown)}")
    values = dict(defaults)
    values.update(raw)

    for key in _POSITIVE_INT:
        if not isinstance(values[key], int) or isinstance(values[key], bool) \
                or values[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    for key in _POSITIVE_FLOAT:
        value = values[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) \
                or not math.isfinite(float(value)) or value <= 0:
            raise ValueError(f"{key} must be a positive finite number")
    if values["image_width"] % values["grid_cols"] != 0:
        raise ValueError("image_width must be divisible by grid_cols")
    if values["image_height"] % values["grid_rows"] != 0:
        raise ValueError("image_height must be divisible by grid_rows")
    if values["feature_dim"] % 6 != 0:
        raise ValueError("feature_dim must be a multiple of 6")
    if values["quantization_levels"] < 2:
        raise ValueError("quantization_levels must be >= 2")

    return FusionConfig(**values)
