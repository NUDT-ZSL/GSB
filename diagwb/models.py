"""Unified internal representation shared by every pipeline stage.

Three stored on-disk formats (v1/v2/v3) are normalized into the same set of
dataclasses:

* :class:`ResultNode`     - the normalized tool result,
* :class:`ShardRef`       - a shard reference node,
* :class:`LineageNode`    - a provenance / source-chain node.

Migration bookkeeping (synthesized fields, discarded fields) and downstream
assembly artifacts (fragments, DAG nodes, manifests) live here too. Every
dataclass is JSON round-trippable so it can be persisted in the cache and in
snapshots.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def _round_conf(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 6)


@dataclass
class LineageNode:
    """One hop in a result's source chain."""

    node_id: str
    source: str
    detail: str = ""
    upstream: List[str] = field(default_factory=list)


@dataclass
class ShardRef:
    """A shard reference node belonging to a result."""

    shard_id: str
    order: int
    text: str
    source: str = "native"


@dataclass
class CompletionRecord:
    """Audit entry for a field synthesized during an upgrade migration."""

    field_name: str
    action: str          # synthesized | inherited | preserved
    source: str
    reason: str


@dataclass
class DiscardRecord:
    """Audit entry for information that cannot survive a downgrade."""

    item_type: str       # shard_ref | aggregation | lineage | confidence
    item_id: str
    rule: str            # flatten | drop
    detail: str


@dataclass
class ResultNode:
    """Normalized, version-agnostic representation of a tool result."""

    result_ref: str
    tool_id: str
    payload: Any
    status: str
    result_format_version: int
    schema_version: str
    confidence: Optional[Tuple[float, float]]
    shards: List[ShardRef] = field(default_factory=list)
    aggregation: Optional[Dict[str, Any]] = None
    lineage: List[LineageNode] = field(default_factory=list)
    raw_content_hash: str = ""
    structure_digest: str = ""
    native_capability: bool = True
    completions: List[CompletionRecord] = field(default_factory=list)
    discards: List[DiscardRecord] = field(default_factory=list)

    def shard_ids(self) -> List[str]:
        return [shard.shard_id for shard in self.shards]

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if self.confidence is not None:
            data["confidence"] = [_round_conf(self.confidence[0]),
                                  _round_conf(self.confidence[1])]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResultNode":
        confidence = data.get("confidence")
        if confidence is not None:
            confidence = (float(confidence[0]), float(confidence[1]))
        return cls(
            result_ref=data["result_ref"],
            tool_id=data["tool_id"],
            payload=data["payload"],
            status=data["status"],
            result_format_version=int(data["result_format_version"]),
            schema_version=data["schema_version"],
            confidence=confidence,
            shards=[ShardRef(**item) for item in data.get("shards", [])],
            aggregation=data.get("aggregation"),
            lineage=[LineageNode(**item) for item in data.get("lineage", [])],
            raw_content_hash=data.get("raw_content_hash", ""),
            structure_digest=data.get("structure_digest", ""),
            native_capability=data.get("native_capability", True),
            completions=[CompletionRecord(**item)
                         for item in data.get("completions", [])],
            discards=[DiscardRecord(**item)
                      for item in data.get("discards", [])],
        )


@dataclass
class ParseRecord:
    """The raw record produced by a format reader, pre-normalization."""

    result_ref: str
    result_format_version: int
    raw_content_hash: str
    fields: Dict[str, Any]
    line_no: int


@dataclass
class ToolCall:
    """One planned tool call from the request manifest."""

    tool_id: str
    arg_digest: str
    result_ref: str
    result_format_version: int


@dataclass
class ManifestEntry:
    """A single multi-turn request from the JSONL manifest."""

    request_id: str
    turn_no: int
    query_text: str
    tool_calls: List[ToolCall]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ManifestEntry":
        return cls(
            request_id=data["request_id"],
            turn_no=int(data["turn_no"]),
            query_text=data["query_text"],
            tool_calls=[ToolCall(**call) for call in data["tool_calls"]],
        )


@dataclass
class Fragment:
    """An answer fragment with full provenance for replay and staleness checks."""

    fragment_id: str
    tool_id: str
    result_ref: str
    result_format_version: int
    schema_version: str
    node_path: str
    cache_key: str
    parse_cache_key: str
    arg_digest: str
    position: int
    interval: Tuple[int, int]
    score: float
    entry_version: int
    text: str
    raw_content_hash: str

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["interval"] = list(self.interval)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Fragment":
        data = dict(data)
        data["interval"] = (int(data["interval"][0]), int(data["interval"][1]))
        return cls(**data)


@dataclass
class DAGNode:
    """A node in the assembly computation graph."""

    node_id: str
    kind: str                       # load | migrate | parse | align | merge | assemble
    inputs: List[str]
    output_hash: str
    status: str                     # hit | recomputed | error
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AssemblyResult:
    """The full outcome of assembling one request."""

    request_id: str
    answer: str
    answer_hash: str
    fragments: List[Fragment]
    dag: List[DAGNode]
    failures: List[str] = field(default_factory=list)
    reused: List[str] = field(default_factory=list)
    recomputed: List[str] = field(default_factory=list)
    affected_results: List[str] = field(default_factory=list)
    affected_fragments: List[str] = field(default_factory=list)
    recompute_suffix: List[str] = field(default_factory=list)

    def fragments_to_dict(self) -> List[Dict[str, Any]]:
        return [fragment.to_dict() for fragment in self.fragments]
