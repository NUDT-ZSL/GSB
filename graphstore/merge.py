from __future__ import annotations

from typing import Any

from .checksums import canonical_json
from .model import (
    SOURCE_PRIORITY,
    ClockConfig,
    GraphIndex,
    MergeAudit,
    SourceVersion,
    UnifiedEdge,
    UnifiedNode,
    V1Snapshot,
    V2Snapshot,
    build_label_index,
)


def _candidate_rank(candidate: dict[str, Any]) -> tuple:
    source = candidate["source"]
    return (
        source["logical_clock_ms"],
        source["shard_generation"],
        source["format_priority"],
        source["snapshot_version"],
        1 if not candidate["tombstone"] else 1,
        candidate.get("weight", 0.0),
    )


def _node_reason(winner: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    if len(candidates) == 1:
        return "single_source"
    source = winner["source"]
    return (
        "tiered_latest:"
        f"logical_clock_ms={source['logical_clock_ms']},"
        f"shard_generation={source['shard_generation']},"
        f"format_priority={source['format_priority']}"
    )


def _edge_candidate_rank(candidate: dict[str, Any]) -> tuple:
    source = candidate["source"]
    return (
        source["logical_clock_ms"],
        source["shard_generation"],
        source["format_priority"],
        candidate["weight"],
        source["snapshot_version"],
    )


def _edge_reason(winner: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    if len(candidates) == 1:
        return "single_source"
    same_source = len(
        {canonical_json(candidate["source"]) for candidate in candidates}
    ) == 1
    source = winner["source"]
    prefix = "same_source_weight_tiebreak" if same_source else "tiered_latest"
    return (
        f"{prefix}:"
        f"logical_clock_ms={source['logical_clock_ms']},"
        f"shard_generation={source['shard_generation']},"
        f"format_priority={source['format_priority']},"
        f"weight={winner['weight']}"
    )


def merge_snapshots(
    v1: V1Snapshot, v2: V2Snapshot, clock: ClockConfig
) -> GraphIndex:
    audit = MergeAudit()
    node_candidates: dict[str, list[dict[str, Any]]] = {}
    edge_candidates: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    v1_source = SourceVersion(
        format="v1",
        snapshot_version=v1.snapshot_version,
        logical_clock_ms=v1.logical_clock_ms,
        shard_generation=0,
        format_priority=SOURCE_PRIORITY["v1"],
    )
    for node in v1.nodes:
        written_at_ms = clock.v1_relative_to_absolute(node.relative_write_seconds)
        source_dict = v1_source.to_dict()
        candidate = {
            "generation": 0,
            "label": node.label,
            "node_id": node.node_id,
            "source": source_dict,
            "tombstone": False,
            "weight": node.weight,
            "written_at_ms": written_at_ms,
        }
        node_candidates.setdefault(node.node_id, []).append(candidate)
        audit.migrations.append(
            {
                "absolute_written_at_ms": written_at_ms,
                "anchor_ms": clock.anchor_ms,
                "filled_generation": 0,
                "filled_tombstone": False,
                "kind": "v1_time_and_semantic_fill",
                "node_id": node.node_id,
                "relative_write_seconds": node.relative_write_seconds,
                "rounding": clock.v1_rounding,
                "scale": {
                    "denominator": clock.v1_scale_denominator,
                    "numerator": clock.v1_scale_numerator,
                },
                "visibility": "visible",
            }
        )
        for edge in node.edges:
            edge_candidate = {
                "from_id": node.node_id,
                "label": edge.label,
                "source": source_dict,
                "to_id": edge.to_id,
                "weight": edge.weight,
            }
            edge_candidates.setdefault(
                (node.node_id, edge.to_id, edge.label), []
            ).append(edge_candidate)

    for shard in v2.shards:
        source = SourceVersion(
            format="v2",
            snapshot_version=v2.snapshot_version,
            logical_clock_ms=v2.logical_clock_ms,
            shard_generation=shard.generation,
            format_priority=SOURCE_PRIORITY["v2"],
        )
        source_dict = source.to_dict()
        for node in shard.nodes:
            candidate = {
                "generation": node.generation,
                "label": node.label,
                "node_id": node.canonical_id,
                "source": source_dict,
                "tombstone": node.tombstone,
                "weight": 0.0,
                "written_at_ms": node.written_at_ms,
            }
            node_candidates.setdefault(node.canonical_id, []).append(candidate)
            for edge in node.out_edges:
                edge_candidate = {
                    "from_id": node.canonical_id,
                    "label": edge.label,
                    "source": source_dict,
                    "to_id": edge.to_id,
                    "weight": edge.weight,
                }
                edge_candidates.setdefault(
                    (node.canonical_id, edge.to_id, edge.label), []
                ).append(edge_candidate)

    merged_nodes: dict[str, UnifiedNode] = {}
    for node_id in sorted(node_candidates):
        candidates = sorted(
            node_candidates[node_id], key=lambda item: canonical_json(item)
        )
        winner = max(candidates, key=_candidate_rank)
        reason = _node_reason(winner, candidates)
        winner_source = SourceVersion(**winner["source"])
        merged_nodes[node_id] = UnifiedNode(
            node_id=node_id,
            label=winner["label"],
            weight=float(winner["weight"]),
            written_at_ms=int(winner["written_at_ms"]),
            tombstone=bool(winner["tombstone"]),
            generation=int(winner["generation"]),
            source=winner_source,
            decision_reason=reason,
            candidates=tuple(candidates),
        )
        audit.node_decisions.append(
            {
                "candidates": candidates,
                "kind": "node",
                "node_id": node_id,
                "reason": reason,
                "winner_source": winner["source"],
            }
        )

    merged_edges: dict[tuple[str, str], UnifiedEdge] = {}
    for key in sorted(edge_candidates):
        from_id, to_id, label = key
        if merged_nodes[from_id].tombstone:
            continue
        candidates = sorted(
            edge_candidates[key], key=lambda item: canonical_json(item)
        )
        winner = max(candidates, key=_edge_candidate_rank)
        reason = _edge_reason(winner, candidates)
        merged_edges[(from_id, to_id, label)] = UnifiedEdge(
            from_id=from_id,
            to_id=to_id,
            label=label,
            weight=float(winner["weight"]),
            source=SourceVersion(**winner["source"]),
            decision_reason=reason,
            sources=tuple(candidates),
        )
        audit.edge_decisions.append(
            {
                "candidates": candidates,
                "edge": {"from_id": from_id, "label": label, "to_id": to_id},
                "kind": "edge",
                "reason": reason,
                "winner": winner,
            }
        )

    adjacency_lists: dict[str, list[UnifiedEdge]] = {}
    for edge in merged_edges.values():
        adjacency_lists.setdefault(edge.from_id, []).append(edge)
    adjacency = {
        from_id: tuple(sorted(edges, key=lambda edge: edge.sort_key()))
        for from_id, edges in sorted(adjacency_lists.items())
    }
    labels, nodes_by_label = build_label_index(merged_nodes)
    return GraphIndex(
        nodes=merged_nodes,
        adjacency=adjacency,
        labels=labels,
        nodes_by_label=nodes_by_label,
        audit=audit,
    )

