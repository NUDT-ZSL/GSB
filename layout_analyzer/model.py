from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


TYPE_LABELS = {
    "body_column": "正文列",
    "inline_annotation": "双行夹注",
    "header_note": "眉批",
    "illustration": "插图",
    "seal": "印章",
}


def type_label(value: str) -> str:
    return TYPE_LABELS.get(value, value)


@dataclass
class Block:
    uid: str
    bbox: tuple[float, float, float, float]
    block_type: str
    confidence: float
    segmentation_confidence: float
    features: dict[str, Any]
    evidence: dict[str, Any]
    component_count: int
    order_index: int | None = None

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def x_center(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2.0

    @property
    def y_center(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "bbox": [round(v, 3) for v in self.bbox],
            "type": self.block_type,
            "type_zh": type_label(self.block_type),
            "order_index": self.order_index,
            "confidence": round(self.confidence, 4),
            "segmentation_confidence": round(self.segmentation_confidence, 4),
            "component_count": self.component_count,
            "features": _round_tree(self.features),
            "evidence": _round_tree(self.evidence),
        }


@dataclass
class GraphEdge:
    source: str
    target: str
    rule: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.source,
            "to": self.target,
            "rule": self.rule,
            "evidence": _round_tree(self.evidence),
            "type": "dependency",
        }


@dataclass
class OrderConflict:
    kind: str
    involved: list[str]
    message: str
    candidate_edges: list[GraphEdge] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "involved": self.involved,
            "message": self.message,
            "candidate_edges": [edge.to_dict() for edge in self.candidate_edges],
        }


@dataclass
class OrderResult:
    order: list[str]
    edges: list[GraphEdge]
    unresolved: list[str]
    conflicts: list[OrderConflict]

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "edges": [edge.to_dict() for edge in self.edges],
            "unresolved_blocks": self.unresolved,
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
        }


@dataclass
class VersionAnalysis:
    page_id: str
    format_name: str
    image_path: str
    image_size: tuple[int, int]
    reference_size: tuple[int, int]
    blocks: list[Block]
    order: OrderResult
    projection: dict[str, Any]

    def features_dict(self) -> dict[str, Any]:
        return {
            "schema": "gsb-layout-features/v1",
            "page_id": self.page_id,
            "format": self.format_name,
            "reference_size": list(self.reference_size),
            "image_size": list(self.image_size),
            "blocks": [
                {
                    "uid": block.uid,
                    "bbox": [round(v, 3) for v in block.bbox],
                    "type": block.block_type,
                    "type_zh": type_label(block.block_type),
                    "width": round(block.width, 3),
                    "height": round(block.height, 3),
                    "x_center": round(block.x_center, 3),
                    "y_center": round(block.y_center, 3),
                    "features": _round_tree(block.features),
                }
                for block in self.blocks
            ],
        }

    def report_dict(self) -> dict[str, Any]:
        by_uid = {block.uid: block for block in self.blocks}
        sequence = []
        for index, uid in enumerate(self.order.order):
            block = by_uid[uid]
            sequence.append(
                {
                    "index": index,
                    "uid": uid,
                    "type": block.block_type,
                    "type_zh": type_label(block.block_type),
                    "bbox": [round(v, 3) for v in block.bbox],
                    "rule_basis": block.evidence,
                }
            )
        return {
            "schema": "gsb-layout-page-report/v1",
            "page_id": self.page_id,
            "format": self.format_name,
            "reference_size": list(self.reference_size),
            "image_size": list(self.image_size),
            "blocks": [block.to_dict() for block in self.blocks],
            "reading_order": sequence,
            "adjacency_graph": self.order.to_dict(),
            "conflict_section": {
                "unresolved_blocks": self.order.unresolved,
                "conflicts": [conflict.to_dict() for conflict in self.order.conflicts],
            },
            "projection": _round_tree(self.projection),
        }


def _round_tree(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {key: _round_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_round_tree(item) for item in value]
    return value
