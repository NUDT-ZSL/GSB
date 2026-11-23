from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .models import (
    MERGE_SCHEMA,
    Edge,
    MergedEdge,
    MergedNode,
    MergeResult,
    NodeVersion,
    PackageManifest,
    SnapshotV1,
    SnapshotV2,
)
from .util import digest_json


def _edge_candidate_trace(edge: Edge, *, winner: bool) -> dict[str, Any]:
    return {
        "source": edge.source,
        "source_priority": edge.source_priority,
        "logical_clock": edge.logical_clock,
        "generation": edge.generation,
        "weight": edge.weight,
        "winner": winner,
    }


def _merge_node(node_id: str, versions: list[NodeVersion]) -> tuple[MergedNode, dict[str, Any]]:
    ordered_versions = sorted(
        versions, key=lambda node: node.version_key, reverse=True
    )
    winner = ordered_versions[0]

    edge_candidates: dict[tuple[str, str], list[Edge]] = defaultdict(list)
    for version in ordered_versions:
        if version.tombstone:
            continue
        for edge in version.edges:
            edge_candidates[edge.key].append(edge)

    merged_edges: list[MergedEdge] = []
    edge_decisions: list[dict[str, Any]] = []
    for key in sorted(edge_candidates):
        candidates = sorted(
            edge_candidates[key],
            key=lambda edge: edge.version_key,
            reverse=True,
        )
        chosen = candidates[0]
        distinct_weights = {float(edge.weight) for edge in candidates}
        conflict = len(candidates) > 1 and len(distinct_weights) > 1
        candidate_traces = tuple(
            _edge_candidate_trace(edge, winner=edge is chosen)
            for edge in candidates
        )
        sources = tuple(sorted({edge.source for edge in candidates}))
        source_versions = tuple(
            {
                "source": edge.source,
                "logical_clock": edge.logical_clock,
                "generation": edge.generation,
                "weight": edge.weight,
            }
            for edge in candidates
        )
        merged = MergedEdge(
            target=chosen.target,
            label=chosen.label,
            weight=chosen.weight,
            sources=sources,
            source_versions=source_versions,
            winner_source=chosen.source,
            winner_logical_clock=chosen.logical_clock,
            winner_generation=chosen.generation,
            conflict=conflict,
            candidates=candidate_traces,
        )
        merged_edges.append(merged)
        if len(candidates) > 1:
            edge_decisions.append(
                {
                    "node_id": node_id,
                    "target": chosen.target,
                    "label": chosen.label,
                    "winner_source": chosen.source,
                    "winner_logical_clock": chosen.logical_clock,
                    "winner_generation": chosen.generation,
                    "winner_weight": chosen.weight,
                    "conflict": conflict,
                    "candidates": list(candidate_traces),
                    "reason": "max(logical_clock, generation, source_priority, source)",
                }
            )

    winner_reason = (
        "max(logical_clock, generation, source_priority, source); "
        f"winner={winner.source}@{winner.logical_clock}/gen{winner.generation}"
    )
    merged_node = MergedNode(
        node_id=node_id,
        label=winner.label,
        weight=winner.weight,
        logical_clock=winner.logical_clock,
        generation=winner.generation,
        source=winner.source,
        tombstone=winner.tombstone,
        edges=tuple(sorted(merged_edges, key=lambda edge: (edge.target, edge.label))),
        versions=tuple(version.to_trace() for version in ordered_versions),
        winner_reason=winner_reason,
        conversion=winner.conversion,
    )
    decision = {
        "node_id": node_id,
        "winner_source": winner.source,
        "winner_logical_clock": winner.logical_clock,
        "winner_generation": winner.generation,
        "winner_tombstone": winner.tombstone,
        "winner_reason": winner_reason,
        "candidates": [version.to_trace() for version in ordered_versions],
    }
    return merged_node, decision


def merge_snapshots(
    v1: SnapshotV1,
    v2: SnapshotV2,
    package_manifest: PackageManifest,
) -> MergeResult:
    by_node: dict[str, list[NodeVersion]] = defaultdict(list)
    for node in v1.nodes.values():
        by_node[node.node_id].append(node)
    for node in v2.nodes.values():
        by_node[node.node_id].append(node)

    merged_nodes: dict[str, MergedNode] = {}
    node_decisions: list[dict[str, Any]] = []
    edge_decisions: list[dict[str, Any]] = []
    for node_id in sorted(by_node):
        node, decision = _merge_node(node_id, by_node[node_id])
        merged_nodes[node_id] = node
        node_decisions.append(decision)
        for edge in node.edges:
            if len(edge.sources) > 1:
                edge_decisions.append(
                    {
                        "node_id": node_id,
                        "target": edge.target,
                        "label": edge.label,
                        "winner_source": edge.winner_source,
                        "winner_logical_clock": edge.winner_logical_clock,
                        "winner_generation": edge.winner_generation,
                        "winner_weight": edge.weight,
                        "conflict": edge.conflict,
                        "sources": list(edge.sources),
                        "source_versions": list(edge.source_versions),
                        "candidates": list(edge.candidates),
                        "reason": "max(logical_clock, generation, source_priority, source)",
                    }
                )

    merge_payload = {
        "schema": MERGE_SCHEMA,
        "snapshot_identity": package_manifest.snapshot_identity,
        "index_identity": package_manifest.index_identity,
        "nodes": [
            merged_nodes[node_id].to_index_dict()
            for node_id in sorted(merged_nodes)
        ],
        "edges": [
            edge.to_dict()
            for node_id in sorted(merged_nodes)
            for edge in merged_nodes[node_id].edges
        ],
        "node_decisions": node_decisions,
        "edge_decisions": edge_decisions,
    }
    return MergeResult(
        nodes=merged_nodes,
        node_decisions=tuple(node_decisions),
        edge_decisions=tuple(edge_decisions),
        package_manifest=package_manifest,
        merge_digest=digest_json(merge_payload),
    )


def merged_node_from_storage(
    node_data: dict[str, Any],
    edges: Iterable[dict[str, Any]],
) -> MergedNode:
    merged_edges = tuple(
        MergedEdge(
            target=str(item["target"]),
            label=str(item["label"]),
            weight=float(item["weight"]),
            sources=tuple(item.get("sources", ())),
            source_versions=tuple(item.get("source_versions", ())),
            winner_source=str(item["winner_source"]),
            winner_logical_clock=int(item["winner_logical_clock"]),
            winner_generation=int(item["winner_generation"]),
            conflict=bool(item.get("conflict", False)),
            candidates=tuple(item.get("candidates", ())),
        )
        for item in edges
    )
    return MergedNode(
        node_id=str(node_data["node_id"]),
        label=str(node_data["label"]),
        weight=(
            float(node_data["weight"])
            if node_data.get("weight") is not None
            else None
        ),
        logical_clock=int(node_data["logical_clock"]),
        generation=int(node_data["generation"]),
        source=str(node_data["source"]),
        tombstone=bool(node_data["tombstone"]),
        edges=tuple(sorted(merged_edges, key=lambda edge: (edge.target, edge.label))),
        versions=tuple(node_data.get("versions", ())),
        winner_reason=str(node_data.get("winner_reason", "")),
        conversion=node_data.get("conversion"),
    )

