"""Graph edit operations used by the batch editor and interactive shell."""

from __future__ import annotations

from typing import Any

from .errors import BusinessError, E_GRAPH_INVALID
from .util import deep_copy_json


SUPPORTED_EDIT_OPS = {
    "update_params",
    "set_enabled",
    "add_node",
    "delete_node",
    "replace_subgraph",
    "add_edge",
    "remove_edge",
    "set_outputs",
    "replace_graph",
}


def _require_graph(raw: dict[str, Any]) -> None:
    if not isinstance(raw, dict):
        raise BusinessError(E_GRAPH_INVALID, "current graph is malformed", stage="edit")


def _find_node(raw: dict[str, Any], node_id: str) -> dict[str, Any]:
    for node in raw.get("nodes", []):
        if isinstance(node, dict) and node.get("id") == node_id:
            return node
    raise BusinessError(E_GRAPH_INVALID, f"node {node_id!r} does not exist", node_id=node_id, stage="edit")


def _remove_node(raw: dict[str, Any], node_id: str) -> None:
    raw["nodes"] = [node for node in raw.get("nodes", []) if not (isinstance(node, dict) and node.get("id") == node_id)]
    raw["edges"] = [
        edge
        for edge in raw.get("edges", [])
        if not (
            isinstance(edge, dict)
            and (
                edge.get("from", edge.get("source")) == node_id
                or edge.get("to", edge.get("target")) == node_id
            )
        )
    ]
    if isinstance(raw.get("outputs"), list):
        raw["outputs"] = [item for item in raw["outputs"] if item != node_id]


def apply_edit_operation(raw_graph: dict[str, Any], operation: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    _require_graph(raw_graph)
    if not isinstance(operation, dict):
        raise BusinessError(E_GRAPH_INVALID, "edit operation must be an object", stage="edit")
    op = operation.get("op")
    if op not in SUPPORTED_EDIT_OPS:
        raise BusinessError(E_GRAPH_INVALID, f"unsupported edit operation {op!r}", stage="edit")
    graph = deep_copy_json(raw_graph)
    edit_id = operation.get("id", f"edit-{op}")
    description = operation.get("description", "")
    if op == "update_params":
        node = _find_node(graph, operation.get("node_id"))
        patch = operation.get("params", operation.get("set", {}))
        if not isinstance(patch, dict):
            raise BusinessError(E_GRAPH_INVALID, "update_params requires a params object", stage="edit")
        current = node.get("params", {})
        if not isinstance(current, dict):
            raise BusinessError(E_GRAPH_INVALID, "node params are malformed", node_id=node.get("id"), stage="edit")
        node["params"] = deep_copy_json(patch) if operation.get("replace") else {**current, **deep_copy_json(patch)}
    elif op == "set_enabled":
        node = _find_node(graph, operation.get("node_id"))
        enabled = operation.get("enabled")
        if not isinstance(enabled, bool):
            raise BusinessError(E_GRAPH_INVALID, "set_enabled requires boolean enabled", stage="edit")
        node["enabled"] = enabled
    elif op == "add_node":
        node = operation.get("node")
        if not isinstance(node, dict):
            raise BusinessError(E_GRAPH_INVALID, "add_node requires a node object", stage="edit")
        graph.setdefault("nodes", []).append(deep_copy_json(node))
        for edge in operation.get("edges", []) or []:
            if not isinstance(edge, dict):
                raise BusinessError(E_GRAPH_INVALID, "add_node edges must be objects", stage="edit")
            graph.setdefault("edges", []).append(deep_copy_json(edge))
        if "outputs" in operation:
            graph["outputs"] = deep_copy_json(operation["outputs"])
    elif op == "delete_node":
        node_id = operation.get("node_id")
        if not isinstance(node_id, str):
            raise BusinessError(E_GRAPH_INVALID, "delete_node requires node_id", stage="edit")
        _find_node(graph, node_id)
        _remove_node(graph, node_id)
    elif op == "replace_subgraph":
        remove_nodes = operation.get("remove_nodes", [])
        if not isinstance(remove_nodes, list) or any(not isinstance(item, str) for item in remove_nodes):
            raise BusinessError(E_GRAPH_INVALID, "replace_subgraph.remove_nodes must be an array of node IDs", stage="edit")
        for node_id in remove_nodes:
            if any(isinstance(node, dict) and node.get("id") == node_id for node in graph.get("nodes", [])):
                _remove_node(graph, node_id)
        add_nodes = operation.get("add_nodes", [])
        if not isinstance(add_nodes, list) or any(not isinstance(item, dict) for item in add_nodes):
            raise BusinessError(E_GRAPH_INVALID, "replace_subgraph.add_nodes must be an array of node objects", stage="edit")
        graph.setdefault("nodes", []).extend(deep_copy_json(add_nodes))
        add_edges = operation.get("add_edges", [])
        if not isinstance(add_edges, list) or any(not isinstance(item, dict) for item in add_edges):
            raise BusinessError(E_GRAPH_INVALID, "replace_subgraph.add_edges must be an array of edge objects", stage="edit")
        for edge in add_edges:
            graph.setdefault("edges", []).append(deep_copy_json(edge))
        if "outputs" in operation:
            graph["outputs"] = deep_copy_json(operation["outputs"])
    elif op == "add_edge":
        edge = operation.get("edge")
        if not isinstance(edge, dict):
            raise BusinessError(E_GRAPH_INVALID, "add_edge requires an edge object", stage="edit")
        graph.setdefault("edges", []).append(deep_copy_json(edge))
    elif op == "remove_edge":
        edge_id = operation.get("edge_id")
        if not isinstance(edge_id, str):
            raise BusinessError(E_GRAPH_INVALID, "remove_edge requires edge_id", stage="edit")
        graph["edges"] = [
            edge for edge in graph.get("edges", []) if not (isinstance(edge, dict) and edge.get("id") == edge_id)
        ]
    elif op == "set_outputs":
        outputs = operation.get("outputs")
        if not isinstance(outputs, list) or any(not isinstance(item, str) for item in outputs):
            raise BusinessError(E_GRAPH_INVALID, "set_outputs requires an outputs array", stage="edit")
        graph["outputs"] = deep_copy_json(outputs)
    elif op == "replace_graph":
        replacement = operation.get("graph")
        if not isinstance(replacement, dict):
            raise BusinessError(E_GRAPH_INVALID, "replace_graph requires a graph object", stage="edit")
        graph = deep_copy_json(replacement)
    return graph, {"id": edit_id, "op": op, "description": description}
