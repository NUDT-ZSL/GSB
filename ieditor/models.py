"""Unified in-memory project model shared by both exchange formats."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import numpy as np

MASK_DAMAGED = 255
MASK_INTACT = 0


@dataclass
class PaletteEntry:
    name: str
    color: List[int]  # deterministic 0..255 RGB triple

    def to_dict(self) -> dict:
        return {"name": self.name, "color": [int(c) for c in self.color]}

    @classmethod
    def from_dict(cls, data: dict) -> "PaletteEntry":
        return cls(name=str(data["name"]), color=[int(c) for c in data["color"]])


@dataclass
class StyleParams:
    """Version-neutral style representation.

    intensity is always present (v1 native value, v2 folded value).
    channel_weights is the fine-grained v2 representation; for v1 projects it
    is populated deterministically through the version mapping table.
    """

    intensity: int
    channel_weights: Dict[str, float]
    palette: List[PaletteEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "intensity": int(self.intensity),
            "channel_weights": {k: float(v) for k, v in
                                sorted(self.channel_weights.items())},
            "palette": [p.to_dict() for p in self.palette],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StyleParams":
        return cls(
            intensity=int(data["intensity"]),
            channel_weights={k: float(v) for k, v in
                             data["channel_weights"].items()},
            palette=[PaletteEntry.from_dict(p) for p in data.get("palette", [])],
        )


@dataclass
class LossEntry:
    kind: str
    detail: str
    values: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "LossEntry":
        return cls(kind=data["kind"], detail=data["detail"],
                   values=dict(data.get("values", {})))


@dataclass
class UnifiedProject:
    project_id: str
    source_version: int
    image_path: str
    style_ref_path: str
    mask: np.ndarray            # uint8 HxW, 0 intact / 255 damaged
    style: StyleParams
    meta: Dict[str, Any] = field(default_factory=dict)
    loss_log: List[LossEntry] = field(default_factory=list)
    edit_log: List[dict] = field(default_factory=list)

    def mask_bitmap(self) -> np.ndarray:
        return (self.mask == MASK_DAMAGED).astype(np.uint8)

    def to_fingerprint_dict(self) -> dict:
        """Canonical, path-independent content used for structure fingerprint."""
        return {
            "project_id": self.project_id,
            "source_version": self.source_version,
            "mask_b64_sha256": _bitmap_hash(self.mask),
            "mask_damaged_pixels": int(np.count_nonzero(self.mask == MASK_DAMAGED)),
            "style": self.style.to_dict(),
            "meta": _canonical_meta(self.meta),
            "loss_log": [e.to_dict() for e in self.loss_log],
        }


def _bitmap_hash(mask: np.ndarray) -> str:
    import hashlib
    packed = np.packbits((mask == MASK_DAMAGED).astype(np.uint8))
    return hashlib.sha256(packed.tobytes()).hexdigest()


def _canonical_meta(meta: dict) -> dict:
    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: clean(value[k]) for k in sorted(value)}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, (np.integer,)):
            return int(value)
        if isinstance(value, (np.floating,)):
            return float(value)
        return value
    return clean(meta)
