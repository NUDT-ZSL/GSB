"""Unified intermediate representation (IR) for region descriptions."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RegionDesc:
    """Unified IR: integer pixel bbox + permille confidence scalar.

    Keeps the source version and the original (pre-quantization) precision
    so that conversions remain auditable and boundary diagnostics can
    reference double-precision values.
    """

    inst_id: str
    category: str
    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1 in integer pixels
    conf_permille: int               # 0..1000
    source_version: int              # 1 or 2
    orig_bbox: tuple[float, float, float, float] | None = None
    orig_confidence: float | None = None
    relations: list[str] = field(default_factory=list)
    color_name: str | None = None
    sort_key: tuple = ("", 0)

    def key_fields(self) -> tuple:
        """Key fields that must survive a round-trip conversion."""
        return (self.inst_id, self.category, tuple(self.bbox), self.conf_permille)

    def to_dict(self) -> dict:
        return {
            "inst_id": self.inst_id,
            "category": self.category,
            "bbox": list(self.bbox),
            "conf_permille": self.conf_permille,
            "source_version": self.source_version,
            "orig_bbox": list(self.orig_bbox) if self.orig_bbox else None,
            "orig_confidence": self.orig_confidence,
            "relations": list(self.relations),
            "color_name": self.color_name,
        }
