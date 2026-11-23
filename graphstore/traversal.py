from __future__ import annotations

from typing import Any, Iterable

from .errors import ErrorCode, fail
from .models import MergedEdge, MergedNode, TraversalRequest, UnifiedIndex
from .util import canonical_json


def _incoming_dict(node_id: str, edge: MergedEdge) -> dict[str, Any]:
    return {
        "from": node_id,
        "label": edge.label,
        "weight": edge.weight,
        "source": edge.winner_source,
    }


def _node_entry(node: MergedNode, incoming: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": node.node_id,
        "label": node.label,
        "source": node.source,
        "logical_clock": node.logical_clock,
        "generation": node.generation,
        "incoming": incoming,
    }


def _frontier_fast(
    index: UnifiedIndex,
    frontier: list[str],
    visited: set[str],
    request: TraversalRequest,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    incoming: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
    for node_id in sorted(frontier):
        for edge in index.outgoing(node_id):
            if not request.edge_allowed(edge):
                continue
            target = edge.target
            if target in visited or target not in index.nodes:
                continue
            if index.nodes[target].tombstone:
                continue
            candidate_key = (
                target,
                edge.label,
                edge.weight,
                edge.winner_source,
            )
            current = incoming.get(target)
            if current is None or candidate_key < current[0]:
                incoming[target] = (
                    candidate_key,
                    _incoming_dict(node_id, edge),
                )
    next_ids = sorted(incoming)
    return next_ids, {key: value[1] for key, value in incoming.items()}


def traverse_start_fast(
    index: UnifiedIndex,
    start: str,
    request: TraversalRequest,
) -> dict[str, Any]:
    start_node = index.exact(start)
    visited = {start}
    frontier = [start]
    layers: list[dict[str, Any]] = [
        {"depth": 0, "nodes": [_node_entry(start_node, None)]}
    ]
    ordered = [start]
    for depth in range(1, request.max_depth + 1):
        next_ids, incoming = _frontier_fast(index, frontier, visited, request)
        if not next_ids:
            break
        visited.update(next_ids)
        frontier = next_ids
        ordered.extend(next_ids)
        layers.append(
            {
                "depth": depth,
                "nodes": [
                    _node_entry(index.nodes[node_id], incoming[node_id])
                    for node_id in next_ids
                ],
            }
        )
    return {
        "start": start,
        "depth_limit": request.max_depth,
        "layers": layers,
        "ordered_ids": ordered,
    }


def traverse_start_truth(
    index: UnifiedIndex,
    start: str,
    request: TraversalRequest,
) -> dict[str, Any]:
    """Single-thread node-by-node BFS used as the unique truth."""

    start_node = index.exact(start)
    visited: set[str] = {start}
    frontier = [start]
    layers: list[dict[str, Any]] = [
        {"depth": 0, "nodes": [_node_entry(start_node, None)]}
    ]
    ordered = [start]
    for depth in range(1, request.max_depth + 1):
        candidates: list[tuple[tuple[Any, ...], str, dict[str, Any]]] = []
        for node_id in sorted(frontier):
            current_node = index.nodes[node_id]
            for edge in current_node.edges:
                if not request.edge_allowed(edge):
                    continue
                target = edge.target
                if target in visited or target not in index.nodes:
                    continue
                if index.nodes[target].tombstone:
                    continue
                candidates.append(
                    (
                        (
                            target,
                            edge.label,
                            edge.weight,
                            edge.winner_source,
                        ),
                        target,
                        _incoming_dict(node_id, edge),
                    )
                )
        candidates.sort(key=lambda item: item[0])
        incoming: dict[str, dict[str, Any]] = {}
        for _key, target, value in candidates:
            if target not in incoming:
                incoming[target] = value
        next_ids = sorted(incoming)
        if not next_ids:
            break
        visited.update(next_ids)
        frontier = next_ids
        ordered.extend(next_ids)
        layers.append(
            {
                "depth": depth,
                "nodes": [
                    _node_entry(index.nodes[node_id], incoming[node_id])
                    for node_id in next_ids
                ],
            }
        )
    return {
        "start": start,
        "depth_limit": request.max_depth,
        "layers": layers,
        "ordered_ids": ordered,
    }


def query_exact_fast(index: UnifiedIndex, node_id: str) -> dict[str, Any]:
    node = index.exact(node_id)
    return {
        "mode": "exact",
        "ordered_ids": [node.node_id],
        "nodes": [node.to_public_dict()],
    }


def query_exact_truth(index: UnifiedIndex, node_id: str) -> dict[str, Any]:
    found = [
        index.nodes[item]
        for item in sorted(index.nodes)
        if item == node_id and not index.nodes[item].tombstone
    ]
    if not found:
        raise fail(
            ErrorCode.NODE_NOT_FOUND,
            "visible node does not exist",
            node_id=node_id,
        )
    node = found[0]
    return {
        "mode": "exact",
        "ordered_ids": [node.node_id],
        "nodes": [node.to_public_dict()],
    }


def query_prefix_fast(index: UnifiedIndex, prefix: str) -> dict[str, Any]:
    nodes = index.prefix(prefix)
    return {
        "mode": "prefix",
        "prefix": prefix,
        "ordered_ids": [node.node_id for node in nodes],
        "nodes": [node.to_public_dict() for node in nodes],
    }


def query_prefix_truth(index: UnifiedIndex, prefix: str) -> dict[str, Any]:
    nodes = [
        index.nodes[node_id]
        for node_id in sorted(index.nodes)
        if not index.nodes[node_id].tombstone
        and index.nodes[node_id].label.startswith(prefix)
    ]
    return {
        "mode": "prefix",
        "prefix": prefix,
        "ordered_ids": [node.node_id for node in nodes],
        "nodes": [node.to_public_dict() for node in nodes],
    }


def _flatten_reach(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for layer in result["layers"]:
        for node in layer["nodes"]:
            rows.append(
                {
                    "start": result["start"],
                    "depth": layer["depth"],
                    "id": node["id"],
                    "source": node["source"],
                    "incoming": node["incoming"],
                }
            )
    return rows


def first_reach_difference(
    parallel: dict[str, Any],
    truth: dict[str, Any],
) -> dict[str, Any] | None:
    parallel_rows = _flatten_reach(parallel)
    truth_rows = _flatten_reach(truth)
    for ordinal in range(max(len(parallel_rows), len(truth_rows))):
        left = parallel_rows[ordinal] if ordinal < len(parallel_rows) else None
        right = truth_rows[ordinal] if ordinal < len(truth_rows) else None
        if left != right:
            depth = (
                left["depth"]
                if left is not None
                else right["depth"] if right is not None else None
            )
            return {
                "start": truth["start"],
                "ordinal": ordinal,
                "depth": depth,
                "parallel_node": left["id"] if left else None,
                "truth_node": right["id"] if right else None,
                "parallel_source": left["source"] if left else None,
                "truth_source": right["source"] if right else None,
                "parallel_row": left,
                "truth_row": right,
            }
    return None


def first_query_difference(
    parallel: dict[str, Any],
    truth: dict[str, Any],
) -> dict[str, Any] | None:
    left = parallel["ordered_ids"]
    right = truth["ordered_ids"]
    for ordinal in range(max(len(left), len(right))):
        left_id = left[ordinal] if ordinal < len(left) else None
        right_id = right[ordinal] if ordinal < len(right) else None
        if left_id != right_id:
            return {
                "mode": truth["mode"],
                "ordinal": ordinal,
                "parallel_node": left_id,
                "truth_node": right_id,
                "depth": 0,
                "parallel_source": (
                    parallel["nodes"][ordinal]["source"]
                    if ordinal < len(parallel["nodes"])
                    else None
                ),
                "truth_source": (
                    truth["nodes"][ordinal]["source"]
                    if ordinal < len(truth["nodes"])
                    else None
                ),
            }
    if canonical_json(parallel) != canonical_json(truth):
        return {
            "mode": truth["mode"],
            "ordinal": 0,
            "parallel_node": None,
            "truth_node": None,
            "depth": 0,
            "reason": "payload differs while ordered IDs match",
        }
    return None


def estimate_start_entries(
    index: UnifiedIndex,
    start: str,
    request: TraversalRequest,
) -> int:
    index.exact(start)
    visited = {start}
    frontier = [start]
    entries = 1
    for _depth in range(1, request.max_depth + 1):
        next_ids, _incoming = _frontier_fast(
            index, frontier, visited, request
        )
        entries += sum(
            sum(1 for edge in index.outgoing(node_id) if request.edge_allowed(edge))
            for node_id in frontier
        )
        if not next_ids:
            break
        visited.update(next_ids)
        frontier = next_ids
    return entries


def execute_batch_traversals(
    index: UnifiedIndex,
    request: TraversalRequest,
    starts: Iterable[str],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for start in starts:
        parallel = traverse_start_fast(index, start, request)
        truth = traverse_start_truth(index, start, request)
        difference = first_reach_difference(parallel, truth)
        if difference is not None:
            raise fail(
                ErrorCode.TRAVERSAL_MISMATCH,
                "parallel traversal differs from serial truth",
                first_difference=difference,
                batch_start=start,
            )
        items.append(parallel)
    return {
        "mode": "reach",
        "items": items,
        "ordered_ids": [item_id for item in items for item_id in item["ordered_ids"]],
    }


def execute_query(
    index: UnifiedIndex,
    request: TraversalRequest,
) -> dict[str, Any]:
    if request.mode == "exact":
        parallel = query_exact_fast(index, request.exact_node or "")
        truth = query_exact_truth(index, request.exact_node or "")
    elif request.mode == "prefix":
        parallel = query_prefix_fast(index, request.prefix or "")
        truth = query_prefix_truth(index, request.prefix or "")
    else:
        raise fail(ErrorCode.INVALID_ARGUMENT, "query mode expected")
    difference = first_query_difference(parallel, truth)
    if difference is not None:
        raise fail(
            ErrorCode.TRAVERSAL_MISMATCH,
            "indexed query differs from serial truth",
            first_difference=difference,
        )
    parallel["truth_checked"] = True
    return parallel

