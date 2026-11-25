"""Canonical data models and deterministic serialization helpers."""
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import List, Optional

FLOAT_DIGITS = 6


def norm_float(value):
    """Normalize a float so serialization is byte-identical across runs."""
    return round(float(value), FLOAT_DIGITS)


def canonical_json(obj):
    """Deterministic JSON: sorted keys, tight separators, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(obj):
    """Content-addressed hash over the canonical JSON of *obj*."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def write_atomic(path, text):
    """Whole-file replace via temp file + rename (never in-place patching)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass(frozen=True)
class Candidate:
    image_id: str
    box_idx: int
    x1: float
    y1: float
    x2: float
    y2: float
    score: float
    class_id: int

    def content(self):
        return {
            "x1": norm_float(self.x1),
            "y1": norm_float(self.y1),
            "x2": norm_float(self.x2),
            "y2": norm_float(self.y2),
            "score": norm_float(self.score),
            "class_id": self.class_id,
        }

    def hash(self):
        return content_hash(self.content())


@dataclass(frozen=True)
class GTBox:
    image_id: str
    box_idx: int
    x1: float
    y1: float
    x2: float
    y2: float
    label: int
    difficulty: int
    occluded: bool
    revision: int

    def content(self):
        return {
            "x1": norm_float(self.x1),
            "y1": norm_float(self.y1),
            "x2": norm_float(self.x2),
            "y2": norm_float(self.y2),
            "label": self.label,
            "difficulty": self.difficulty,
            "occluded": self.occluded,
            "revision": self.revision,
        }


@dataclass
class MatchRow:
    """One ledger-visible assignment row (TP/FP candidate or FN ground truth)."""

    image_id: str
    cand_idx: Optional[int]
    gt_idx: Optional[int]
    status: str  # "TP" | "FP" | "FN"
    iou: float
    inter: float
    union: float
    class_id: int
    score: Optional[float]
    cand_hash: Optional[str]
    gt_revision: Optional[int]

    def key(self):
        return (self.image_id, self.cand_idx, self.gt_idx, self.status)


@dataclass
class ImageMatch:
    """Full match result for one image, including the comparison audit log."""

    image_id: str
    rows: List[MatchRow] = field(default_factory=list)
    comparisons: List[dict] = field(default_factory=list)
    aggregate: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "image_id": self.image_id,
            "rows": [dict(row.__dict__) for row in self.rows],
            "comparisons": self.comparisons,
            "aggregate": self.aggregate,
        }

    @staticmethod
    def from_dict(doc):
        im = ImageMatch(image_id=doc["image_id"])
        im.rows = [MatchRow(**r) for r in doc["rows"]]
        im.comparisons = [dict(c) for c in doc["comparisons"]]
        im.aggregate = dict(doc["aggregate"])
        return im
