from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Any, Iterable


SOURCE_PRIORITY = {"v1": 10, "v2": 20}


@dataclass(frozen=True)
class ClockConfig:
    anchor_ms: int
    step_ms: int
    max_rollback_ms: int
    v1_scale_numerator: int = 1000
    v1_scale_denominator: int = 1
    v1_rounding: str = "floor"

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_ms": self.anchor_ms,
            "max_rollback_ms": self.max_rollback_ms,
            "step_ms": self.step_ms,
            "v1_conversion": {
                "denominator": self.v1_scale_denominator,
                "numerator": self.v1_scale_numerator,
                "rounding": self.v1_rounding,
            },
        }

    def v1_relative_to_absolute(self, seconds: int) -> int:
        numerator = seconds * self.v1_scale_numerator
        if self.v1_rounding == "floor":
            return self.anchor_ms + numerator // self.v1_scale_denominator
        if self.v1_rounding == "ceil":
            return self.anchor_ms + (
                numerator + self.v1_scale_denominator - 1
            ) // self.v1_scale_denominator
        raise ValueError(f"unsupported rounding: {self.v1_rounding}")


@dataclass(frozen=True)
class EdgeInput:
    to_id: str
    label: str
    weight: float


@dataclass(frozen=True)
class V1Node:
    node_id: str
    label: str
    weight: float
    relative_write_seconds: int
    edges: tuple[EdgeInput, ...]


@dataclass(frozen=True)
class V1Snapshot:
    snapshot_version: int
    logical_clock_ms: int
    path: str
    sha256: str
    nodes: tuple[V1Node, ...]


@dataclass(frozen=True)
class V2Node:
    canonical_id: str
    label: str
    out_edges: tuple[EdgeInput, ...]
    written_at_ms: int
    generation: int
    tombstone: bool
    state: str
    shard_id: str


@dataclass(frozen=True)
class V2Shard:
    shard_id: str
    generation: int
    content_hash: str
    path: str
    file_sha256: str
    nodes: tuple[V2Node, ...]


@dataclass(frozen=True)
class V2Snapshot:
    snapshot_version: int
    logical_clock_ms: int
    global_generation: int
    adjacency_digest: str
    manifest_path: str
    metadata_path: str
    manifest_sha256: str
    metadata_sha256: str
    shards: tuple[V2Shard, ...]


@dataclass(frozen=True)
class SourceVersion:
    format: str
    snapshot_version: int
    logical_clock_ms: int
    shard_generation: int
    format_priority: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "format_priority": self.format_priority,
            "logical_clock_ms": self.logical_clock_ms,
            "shard_generation": self.shard_generation,
            "snapshot_version": self.snapshot_version,
        }


@dataclass(frozen=True)
class UnifiedNode:
    node_id: str
    label: str
    weight: float
    written_at_ms: int
    tombstone: bool
    generation: int
    source: SourceVersion
    decision_reason: str
    candidates: tuple[dict[str, Any], ...] = ()

    def public_dict(self, *, audit: bool = False) -> dict[str, Any]:
        result = {
            "generation": self.generation,
            "label": self.label,
            "node_id": self.node_id,
            "source": self.source.to_dict(),
            "tombstone": self.tombstone,
            "written_at_ms": self.written_at_ms,
        }
        if audit:
            result["decision_reason"] = self.decision_reason
            result["candidates"] = list(self.candidates)
            result["weight"] = self.weight
        return result


@dataclass(frozen=True)
class UnifiedEdge:
    from_id: str
    to_id: str
    label: str
    weight: float
    source: SourceVersion
    decision_reason: str
    sources: tuple[dict[str, Any], ...] = ()

    def sort_key(self) -> tuple:
        return (
            self.to_id,
            self.label,
            self.weight,
            self.source.format,
            self.source.snapshot_version,
        )


@dataclass
class MergeAudit:
    node_decisions: list[dict[str, Any]] = field(default_factory=list)
    edge_decisions: list[dict[str, Any]] = field(default_factory=list)
    migrations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class GraphIndex:
    nodes: dict[str, UnifiedNode]
    adjacency: dict[str, tuple[UnifiedEdge, ...]]
    labels: tuple[str, ...]
    nodes_by_label: dict[str, tuple[str, ...]]
    audit: MergeAudit
    index_version: int = 0

    @property
    def edge_count(self) -> int:
        return sum(len(edges) for edges in self.adjacency.values())

    @property
    def tombstone_count(self) -> int:
        return sum(1 for node in self.nodes.values() if node.tombstone)

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    def adjacency_rows(self) -> Iterable[tuple[str, str, str, float]]:
        for from_id in sorted(self.adjacency):
            for edge in self.adjacency[from_id]:
                yield (from_id, edge.to_id, edge.label, edge.weight)

    def adjacency_digest(self) -> str:
        from .checksums import digest_edges

        return digest_edges(self.adjacency_rows())

    def exact(self, node_id: str) -> UnifiedNode | None:
        return self.nodes.get(node_id)

    def label_prefix(self, prefix: str) -> tuple[str, ...]:
        start = bisect_left(self.labels, prefix)
        node_ids: list[str] = []
        for label in self.labels[start:]:
            if not label.startswith(prefix):
                break
            node_ids.extend(self.nodes_by_label[label])
        return tuple(sorted(node_ids))

    def neighbors(self, node_id: str) -> tuple[UnifiedEdge, ...]:
        return self.adjacency.get(node_id, ())


def build_label_index(
    nodes: dict[str, UnifiedNode],
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    grouped: dict[str, list[str]] = {}
    for node in nodes.values():
        grouped.setdefault(node.label, []).append(node.node_id)
    labels = tuple(sorted(grouped))
    by_label = {label: tuple(sorted(ids)) for label, ids in grouped.items()}
    return labels, by_label

