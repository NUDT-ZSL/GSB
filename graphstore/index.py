from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .errors import ErrorCode, fail
from .merge import merged_node_from_storage
from .models import (
    INDEX_SCHEMA,
    MergedEdge,
    MergedNode,
    MergeResult,
    PackageManifest,
    UnifiedIndex,
)
from .util import canonical_json, digest_json


def _label_prefixes(label: str) -> tuple[str, ...]:
    if not label:
        return ()
    return tuple(label[:index] for index in range(1, len(label) + 1))


def _assemble_index(
    *,
    index_version: str,
    manifest: PackageManifest,
    nodes: dict[str, MergedNode],
    merge_digest: str,
) -> UnifiedIndex:
    visible_node_ids = tuple(
        node_id for node_id in sorted(nodes) if not nodes[node_id].tombstone
    )
    label_prefix_sets: dict[str, set[str]] = defaultdict(set)
    target_sets: dict[str, set[str]] = defaultdict(set)
    edge_label_sets: dict[str, set[str]] = defaultdict(set)
    adjacency: dict[str, tuple[MergedEdge, ...]] = {}

    for node_id in sorted(nodes):
        node = nodes[node_id]
        if node.tombstone:
            adjacency[node_id] = ()
            continue
        for prefix in _label_prefixes(node.label):
            label_prefix_sets[prefix].add(node_id)
        adjacency[node_id] = tuple(
            sorted(node.edges, key=lambda edge: (edge.target, edge.label))
        )
        for edge in adjacency[node_id]:
            target_sets[edge.target].add(node_id)
            edge_label_sets[edge.label].add(node_id)

    label_prefix_index = {
        key: tuple(sorted(values))
        for key, values in sorted(label_prefix_sets.items())
    }
    target_index = {
        key: tuple(sorted(values))
        for key, values in sorted(target_sets.items())
    }
    edge_label_index = {
        key: tuple(sorted(values))
        for key, values in sorted(edge_label_sets.items())
    }

    visible_nodes = [nodes[node_id] for node_id in visible_node_ids]
    edge_count = sum(len(adjacency[node_id]) for node_id in visible_node_ids)
    summary = {
        "schema": INDEX_SCHEMA,
        "index_version": index_version,
        "snapshot_identity": manifest.snapshot_identity,
        "index_identity": manifest.index_identity,
        "snapshot_id": manifest.snapshot_id,
        "snapshot_version": manifest.package_version,
        "merge_digest": merge_digest,
        "node_count": len(nodes),
        "visible_node_count": len(visible_node_ids),
        "tombstone_count": len(nodes) - len(visible_node_ids),
        "edge_count": edge_count,
        "label_prefixes": sorted(label_prefix_index),
        "nodes": [
            {
                "id": node.node_id,
                "label": node.label,
                "weight": node.weight,
                "logical_clock": node.logical_clock,
                "generation": node.generation,
                "source": node.source,
                "tombstone": node.tombstone,
                "edges": [edge.to_dict() for edge in adjacency[node.node_id]],
            }
            for node in visible_nodes
        ],
        "tombstones": [
            {
                "id": node.node_id,
                "logical_clock": node.logical_clock,
                "generation": node.generation,
                "source": node.source,
                "versions": list(node.versions),
            }
            for node_id in sorted(nodes)
            for node in [nodes[node_id]]
            if node.tombstone
        ],
        "label_prefix_index": {
            key: list(values) for key, values in label_prefix_index.items()
        },
        "out_target_index": {
            key: list(values) for key, values in target_index.items()
        },
        "edge_label_index": {
            key: list(values) for key, values in edge_label_index.items()
        },
    }
    digest = digest_json(summary)
    return UnifiedIndex(
        index_version=index_version,
        manifest=manifest,
        nodes=nodes,
        visible_node_ids=visible_node_ids,
        label_prefix_index=label_prefix_index,
        target_index=target_index,
        edge_label_index=edge_label_index,
        summary=summary,
        digest=digest,
        merge_digest=merge_digest,
        _adjacency=adjacency,
    )


def build_index(merge_result: MergeResult, *, index_version: str) -> UnifiedIndex:
    return _assemble_index(
        index_version=index_version,
        manifest=merge_result.package_manifest,
        nodes=merge_result.nodes,
        merge_digest=merge_result.merge_digest,
    )


def rehydrate_index(
    *,
    index_version: str,
    manifest: PackageManifest,
    node_rows: Iterable[dict[str, Any]],
    edge_rows: Iterable[dict[str, Any]],
    persisted_summary: dict[str, Any],
    persisted_digest: str,
    merge_digest: str,
) -> UnifiedIndex:
    edges_by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in edge_rows:
        edges_by_node[str(row["node_id"])].append(row["edge_json"])
    nodes: dict[str, MergedNode] = {}
    for row in node_rows:
        node_data = row["node_json"]
        node_id = str(node_data["node_id"])
        nodes[node_id] = merged_node_from_storage(
            node_data, edges_by_node.get(node_id, ())
        )
    rebuilt = _assemble_index(
        index_version=index_version,
        manifest=manifest,
        nodes=nodes,
        merge_digest=merge_digest,
    )
    if canonical_json(rebuilt.summary) != canonical_json(persisted_summary):
        raise fail(
            ErrorCode.INDEX_VERIFY_FAILED,
            "persisted index summary does not match its rows",
            index_version=index_version,
        )
    if rebuilt.digest != persisted_digest:
        raise fail(
            ErrorCode.INDEX_VERIFY_FAILED,
            "persisted index digest does not match recomputed digest",
            index_version=index_version,
            expected=rebuilt.digest,
            actual=persisted_digest,
        )
    return rebuilt

