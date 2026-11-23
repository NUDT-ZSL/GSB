from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import ErrorCode, fail
from .util import (
    canonical_json,
    canonical_node_id,
    digest_json,
    finite_number,
    is_int,
    validate_label,
)


SOURCE_PRIORITY = {"v1": 1, "v2": 2}
MERGE_SCHEMA = "graphstore.merge/1"
INDEX_SCHEMA = "graphstore.index/1"


@dataclass(frozen=True)
class Edge:
    target: str
    label: str
    weight: float = 1.0
    source: str = ""
    logical_clock: int = 0
    generation: int = 0
    source_priority: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.target, self.label)

    @property
    def version_key(self) -> tuple[int, int, int, str]:
        return (
            self.logical_clock,
            self.generation,
            self.source_priority,
            self.source,
        )

    def to_dict(self, *, include_version: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "target": self.target,
            "label": self.label,
            "weight": self.weight,
        }
        if include_version:
            result.update(
                {
                    "source": self.source,
                    "logical_clock": self.logical_clock,
                    "generation": self.generation,
                }
            )
        return result


@dataclass(frozen=True)
class NodeVersion:
    node_id: str
    label: str
    weight: float | None
    logical_clock: int
    generation: int
    source: str
    source_priority: int
    tombstone: bool
    edges: tuple[Edge, ...] = ()
    shard_id: str | None = None
    conversion: dict[str, Any] | None = None

    @property
    def version_key(self) -> tuple[int, int, int, str]:
        return (
            self.logical_clock,
            self.generation,
            self.source_priority,
            self.source,
        )

    def to_trace(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_priority": self.source_priority,
            "logical_clock": self.logical_clock,
            "generation": self.generation,
            "tombstone": self.tombstone,
            "label": self.label,
            "weight": self.weight,
            "conversion": self.conversion,
            "shard_id": self.shard_id,
        }


@dataclass(frozen=True)
class MergedEdge:
    target: str
    label: str
    weight: float
    sources: tuple[str, ...]
    source_versions: tuple[dict[str, Any], ...]
    winner_source: str
    winner_logical_clock: int
    winner_generation: int
    conflict: bool
    candidates: tuple[dict[str, Any], ...]

    @property
    def key(self) -> tuple[str, str]:
        return (self.target, self.label)

    @property
    def version_key(self) -> tuple[int, int, int, str]:
        return (
            self.winner_logical_clock,
            self.winner_generation,
            SOURCE_PRIORITY.get(self.winner_source.split(":", 1)[0], 0),
            self.winner_source,
        )

    def to_dict(self, *, include_audit: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "target": self.target,
            "label": self.label,
            "weight": self.weight,
            "sources": list(self.sources),
            "winner_source": self.winner_source,
            "winner_logical_clock": self.winner_logical_clock,
            "winner_generation": self.winner_generation,
        }
        if include_audit:
            result["conflict"] = self.conflict
            result["candidates"] = list(self.candidates)
        return result


@dataclass(frozen=True)
class MergedNode:
    node_id: str
    label: str
    weight: float | None
    logical_clock: int
    generation: int
    source: str
    tombstone: bool
    edges: tuple[MergedEdge, ...]
    versions: tuple[dict[str, Any], ...]
    winner_reason: str
    conversion: dict[str, Any] | None = None

    @property
    def source_priority(self) -> int:
        return SOURCE_PRIORITY.get(self.source.split(":", 1)[0], 0)

    @property
    def version_key(self) -> tuple[int, int, int, str]:
        return (
            self.logical_clock,
            self.generation,
            self.source_priority,
            self.source,
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.node_id,
            "label": self.label,
            "weight": self.weight,
            "logical_clock": self.logical_clock,
            "generation": self.generation,
            "source": self.source,
            "tombstone": self.tombstone,
            "edge_count": len(self.edges),
        }

    def to_index_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "label": self.label,
            "weight": self.weight,
            "logical_clock": self.logical_clock,
            "generation": self.generation,
            "source": self.source,
            "tombstone": self.tombstone,
            "versions": list(self.versions),
            "winner_reason": self.winner_reason,
            "conversion": self.conversion,
        }


@dataclass(frozen=True)
class PackageManifest:
    schema: str
    snapshot_id: str
    package_version: int
    logical_clock_floor: int
    node_count_min: int
    node_count_max: int
    shards: tuple[dict[str, Any], ...]
    v1_snapshot_id: str
    v2_snapshot_id: str
    v1_digest: str
    v2_manifest_digest: str
    v2_metadata_digest: str
    injected_clock_ms: int
    v1_clock_scale_ms: int
    v1_rounding: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PackageManifest":
        return cls(
            schema=str(value["schema"]),
            snapshot_id=str(value["snapshot_id"]),
            package_version=int(value["package_version"]),
            logical_clock_floor=int(value["logical_clock_floor"]),
            node_count_min=int(value["node_count_min"]),
            node_count_max=int(value["node_count_max"]),
            shards=tuple(value["shards"]),
            v1_snapshot_id=str(value["v1_snapshot_id"]),
            v2_snapshot_id=str(value["v2_snapshot_id"]),
            v1_digest=str(value.get("v1_digest", "")),
            v2_manifest_digest=str(value.get("v2_manifest_digest", "")),
            v2_metadata_digest=str(value.get("v2_metadata_digest", "")),
            injected_clock_ms=int(value["injected_clock_ms"]),
            v1_clock_scale_ms=int(value["v1_clock_scale_ms"]),
            v1_rounding=str(value["v1_rounding"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "snapshot_id": self.snapshot_id,
            "package_version": self.package_version,
            "logical_clock_floor": self.logical_clock_floor,
            "node_count_min": self.node_count_min,
            "node_count_max": self.node_count_max,
            "shards": list(self.shards),
            "v1_snapshot_id": self.v1_snapshot_id,
            "v2_snapshot_id": self.v2_snapshot_id,
            "v1_digest": self.v1_digest,
            "v2_manifest_digest": self.v2_manifest_digest,
            "v2_metadata_digest": self.v2_metadata_digest,
            "injected_clock_ms": self.injected_clock_ms,
            "v1_clock_scale_ms": self.v1_clock_scale_ms,
            "v1_rounding": self.v1_rounding,
        }

    @property
    def snapshot_identity(self) -> str:
        """Identity of the supplied snapshot package, independent of ingest knobs."""

        return digest_json(
            {
                "schema": self.schema,
                "snapshot_id": self.snapshot_id,
                "package_version": self.package_version,
                "logical_clock_floor": self.logical_clock_floor,
                "node_count_min": self.node_count_min,
                "node_count_max": self.node_count_max,
                "shards": list(self.shards),
                "v1_snapshot_id": self.v1_snapshot_id,
                "v2_snapshot_id": self.v2_snapshot_id,
                "v1_digest": self.v1_digest,
                "v2_manifest_digest": self.v2_manifest_digest,
                "v2_metadata_digest": self.v2_metadata_digest,
            }
        )

    @property
    def index_identity(self) -> str:
        """Identity of the deterministic index produced from the package."""

        return digest_json(
            {
                "snapshot_identity": self.snapshot_identity,
                "injected_clock_ms": self.injected_clock_ms,
                "v1_clock_scale_ms": self.v1_clock_scale_ms,
                "v1_rounding": self.v1_rounding,
                "merge_schema": MERGE_SCHEMA,
            }
        )

    def shard_hashes(self) -> dict[str, str]:
        return {
            str(item["file"]): str(item["sha256"])
            for item in self.shards
        }


@dataclass(frozen=True)
class SnapshotV1:
    path: str
    schema: str
    snapshot_id: str
    version: int
    nodes: dict[str, NodeVersion]
    linear_index: tuple[str, ...]
    raw_digest: str
    injected_clock_ms: int
    clock_scale_ms: int
    rounding: str

    def summary(self) -> dict[str, Any]:
        return {
            "format": "v1",
            "path": self.path,
            "snapshot_id": self.snapshot_id,
            "version": self.version,
            "node_count": len(self.nodes),
            "edge_count": sum(len(node.edges) for node in self.nodes.values()),
            "injected_clock_ms": self.injected_clock_ms,
            "clock_scale_ms": self.clock_scale_ms,
            "rounding": self.rounding,
            "raw_digest": self.raw_digest,
        }


@dataclass(frozen=True)
class SnapshotV2:
    path: str
    schema: str
    snapshot_id: str
    version: int
    logical_clock_floor: int
    node_count_min: int
    node_count_max: int
    nodes: dict[str, NodeVersion]
    tombstones: tuple[str, ...]
    shards: tuple[dict[str, Any], ...]
    manifest_digest: str
    metadata_digest: str

    def summary(self) -> dict[str, Any]:
        return {
            "format": "v2",
            "path": self.path,
            "snapshot_id": self.snapshot_id,
            "version": self.version,
            "logical_clock_floor": self.logical_clock_floor,
            "node_count": len(self.nodes),
            "tombstone_count": len(self.tombstones),
            "node_count_bounds": [self.node_count_min, self.node_count_max],
            "shard_count": len(self.shards),
            "shards": list(self.shards),
            "manifest_digest": self.manifest_digest,
            "metadata_digest": self.metadata_digest,
        }


@dataclass(frozen=True)
class MergeResult:
    nodes: dict[str, MergedNode]
    node_decisions: tuple[dict[str, Any], ...]
    edge_decisions: tuple[dict[str, Any], ...]
    package_manifest: PackageManifest
    merge_digest: str

    @property
    def visible_node_count(self) -> int:
        return sum(1 for node in self.nodes.values() if not node.tombstone)

    @property
    def visible_edge_count(self) -> int:
        return sum(
            len(node.edges)
            for node in self.nodes.values()
            if not node.tombstone
        )

    def summary(self) -> dict[str, Any]:
        return {
            "schema": MERGE_SCHEMA,
            "snapshot_id": self.package_manifest.snapshot_id,
            "node_count": len(self.nodes),
            "visible_node_count": self.visible_node_count,
            "tombstone_count": sum(
                1 for node in self.nodes.values() if node.tombstone
            ),
            "visible_edge_count": self.visible_edge_count,
            "node_decisions": len(self.node_decisions),
            "edge_decisions": len(self.edge_decisions),
            "merge_digest": self.merge_digest,
        }


@dataclass(frozen=True)
class TraversalRequest:
    mode: str
    starts: tuple[str, ...] = ()
    exact_node: str | None = None
    prefix: str | None = None
    max_depth: int = 2
    edge_labels: tuple[str, ...] = ()
    min_edge_weight: float | None = None
    max_edge_weight: float | None = None
    max_parallel_tasks: int = 2
    memory_budget_entries: int = 1000
    batch_size: int = 2

    @classmethod
    def create(
        cls,
        *,
        mode: str = "reach",
        starts: Iterable[str] = (),
        exact_node: str | None = None,
        prefix: str | None = None,
        max_depth: int = 2,
        edge_labels: Iterable[str] = (),
        min_edge_weight: float | None = None,
        max_edge_weight: float | None = None,
        max_parallel_tasks: int = 2,
        memory_budget_entries: int = 1000,
        batch_size: int = 2,
    ) -> "TraversalRequest":
        if mode not in {"reach", "exact", "prefix"}:
            raise fail(ErrorCode.INVALID_ARGUMENT, "mode must be reach, exact or prefix")
        if not is_int(max_depth) or max_depth <= 0:
            raise fail(ErrorCode.INVALID_DEPTH, "max_depth must be a positive integer")
        if not is_int(max_parallel_tasks) or max_parallel_tasks <= 0:
            raise fail(
                ErrorCode.INVALID_ARGUMENT,
                "max_parallel_tasks must be a positive integer",
            )
        if not is_int(memory_budget_entries) or memory_budget_entries < 0:
            raise fail(
                ErrorCode.INVALID_ARGUMENT,
                "memory_budget_entries must be a non-negative integer",
            )
        if not is_int(batch_size) or batch_size <= 0:
            raise fail(ErrorCode.INVALID_ARGUMENT, "batch_size must be a positive integer")

        starts_tuple = tuple(sorted({canonical_node_id(item) for item in starts}))
        exact = canonical_node_id(exact_node) if exact_node is not None else None
        labels = tuple(sorted({validate_label(item) for item in edge_labels}))

        if prefix is not None:
            if not isinstance(prefix, str) or not prefix:
                raise fail(ErrorCode.INVALID_ARGUMENT, "prefix must not be empty")
        for value, name in ((min_edge_weight, "min_edge_weight"), (max_edge_weight, "max_edge_weight")):
            if value is not None and not finite_number(value):
                raise fail(ErrorCode.INVALID_ARGUMENT, f"{name} must be finite")
        if (
            min_edge_weight is not None
            and max_edge_weight is not None
            and float(min_edge_weight) > float(max_edge_weight)
        ):
            raise fail(
                ErrorCode.INVALID_ARGUMENT,
                "min_edge_weight must not exceed max_edge_weight",
            )
        if mode == "reach" and not starts_tuple:
            raise fail(ErrorCode.INVALID_ARGUMENT, "reach mode requires at least one start")
        if mode == "exact" and exact is None:
            raise fail(ErrorCode.INVALID_ARGUMENT, "exact mode requires exact_node")
        if mode == "prefix" and prefix is None:
            raise fail(ErrorCode.INVALID_ARGUMENT, "prefix mode requires prefix")

        return cls(
            mode=mode,
            starts=starts_tuple,
            exact_node=exact,
            prefix=prefix,
            max_depth=max_depth,
            edge_labels=labels,
            min_edge_weight=float(min_edge_weight) if min_edge_weight is not None else None,
            max_edge_weight=float(max_edge_weight) if max_edge_weight is not None else None,
            max_parallel_tasks=max_parallel_tasks,
            memory_budget_entries=memory_budget_entries,
            batch_size=batch_size,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "starts": list(self.starts),
            "exact_node": self.exact_node,
            "prefix": self.prefix,
            "max_depth": self.max_depth,
            "edge_labels": list(self.edge_labels),
            "min_edge_weight": self.min_edge_weight,
            "max_edge_weight": self.max_edge_weight,
            "max_parallel_tasks": self.max_parallel_tasks,
            "memory_budget_entries": self.memory_budget_entries,
            "batch_size": self.batch_size,
        }

    @property
    def digest(self) -> str:
        return digest_json(self.to_dict())

    def edge_allowed(self, edge: Edge | MergedEdge) -> bool:
        if self.edge_labels and edge.label not in self.edge_labels:
            return False
        weight = float(edge.weight)
        if self.min_edge_weight is not None and weight < self.min_edge_weight:
            return False
        if self.max_edge_weight is not None and weight > self.max_edge_weight:
            return False
        return True

    def canonical(self) -> str:
        return canonical_json(self.to_dict())


@dataclass
class UnifiedIndex:
    index_version: str
    manifest: PackageManifest
    nodes: dict[str, MergedNode]
    visible_node_ids: tuple[str, ...]
    label_prefix_index: dict[str, tuple[str, ...]]
    target_index: dict[str, tuple[str, ...]]
    edge_label_index: dict[str, tuple[str, ...]]
    summary: dict[str, Any]
    digest: str
    merge_digest: str
    _adjacency: dict[str, tuple[MergedEdge, ...]] = field(repr=False)

    def node(self, node_id: str) -> MergedNode:
        return self.nodes[node_id]

    def outgoing(self, node_id: str) -> tuple[MergedEdge, ...]:
        return self._adjacency.get(node_id, ())

    def exact(self, node_id: str) -> MergedNode:
        node = self.nodes.get(node_id)
        if node is None or node.tombstone:
            raise fail(
                ErrorCode.NODE_NOT_FOUND,
                "visible node does not exist",
                node_id=node_id,
            )
        return node

    def prefix(self, prefix: str) -> tuple[MergedNode, ...]:
        ids = self.label_prefix_index.get(prefix, ())
        return tuple(self.nodes[node_id] for node_id in ids)

    def node_count(self) -> int:
        return len(self.nodes)

    def edge_count(self) -> int:
        return sum(len(edges) for edges in self._adjacency.values())


def merged_edge_from_dict(value: dict[str, Any]) -> MergedEdge:
    return MergedEdge(
        target=str(value["target"]),
        label=str(value["label"]),
        weight=float(value["weight"]),
        sources=tuple(value.get("sources", ())),
        source_versions=tuple(value.get("source_versions", ())),
        winner_source=str(value["winner_source"]),
        winner_logical_clock=int(value["winner_logical_clock"]),
        winner_generation=int(value["winner_generation"]),
        conflict=bool(value.get("conflict", False)),
        candidates=tuple(value.get("candidates", ())),
    )


def edge_from_dict(value: dict[str, Any]) -> Edge:
    if not math.isfinite(float(value["weight"])):
        raise fail(ErrorCode.INVALID_ARGUMENT, "edge weight must be finite")
    return Edge(
        target=value["target"],
        label=value["label"],
        weight=float(value["weight"]),
        source=value.get("source", ""),
        logical_clock=int(value.get("logical_clock", 0)),
        generation=int(value.get("generation", 0)),
        source_priority=int(value.get("source_priority", 0)),
    )

