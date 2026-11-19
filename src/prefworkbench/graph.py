from __future__ import annotations

from typing import Any, Callable

from .canonical import hash_value
from .cache import cache_key_from_components
from .nodes import (
    NODE_IMPL_VERSION,
    NODE_ORDER,
    TOPOLOGY_EDGES,
    allocation_sampling,
    near_duplicate,
    pair_consistency,
    quality_clean,
)

NODE_SCHEMA_VERSION = 1


def build_key_components(
    node: str,
    *,
    input_components: list[dict[str, Any]],
    upstream_hashes: dict[str, str],
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = [
        {
            "name": "implementation_version",
            "kind": "constant",
            "value": NODE_IMPL_VERSION,
        },
        {
            "name": "node_schema_version",
            "kind": "constant",
            "value": NODE_SCHEMA_VERSION,
        },
        {
            "name": "node",
            "kind": "constant",
            "value": node,
        },
    ]
    components.extend(input_components)
    for upstream_node in sorted(upstream_hashes):
        components.append(
            {
                "name": f"upstream.{upstream_node}",
                "kind": "upstream_payload_hash",
                "value": upstream_hashes[upstream_node],
            }
        )
    components.append(
        {
            "name": "node_parameters",
            "kind": "parameters",
            "value": params,
        }
    )
    return components


def cache_key_for(components: list[dict[str, Any]]) -> str:
    return cache_key_from_components(components)


def payload_hash(payload: dict[str, Any]) -> str:
    return hash_value(payload)


def compute_node(
    node: str,
    *,
    samples: list[dict[str, Any]],
    source_sha256: str,
    params: dict[str, Any],
    upstream_payloads: dict[str, dict[str, Any]],
    version: int | None = None,
) -> dict[str, Any]:
    if node == "quality_clean":
        return quality_clean(samples, params, source_sha256=source_sha256)
    if node == "near_duplicate":
        return near_duplicate(upstream_payloads["quality_clean"], params, version=version)
    if node == "pair_consistency":
        return pair_consistency(upstream_payloads["near_duplicate"], params, version=version)
    if node == "allocation_sampling":
        return allocation_sampling(upstream_payloads["pair_consistency"], params)
    raise ValueError(node)


def upstream_for(node: str) -> list[str]:
    return [source for source, target in TOPOLOGY_EDGES if target == node]


def topology_is_acyclic(
    nodes: list[str] | None = None,
    edges: list[tuple[str, str]] | None = None,
) -> tuple[bool, list[str]]:
    node_list = list(nodes or NODE_ORDER)
    edge_list = list(edges or TOPOLOGY_EDGES)
    indegree = {node: 0 for node in node_list}
    outgoing: dict[str, list[str]] = {node: [] for node in node_list}
    for source, target in edge_list:
        if source not in indegree or target not in indegree:
            return False, [f"unknown edge node: {source}->{target}"]
        outgoing[source].append(target)
        indegree[target] += 1
    queue = sorted(node for node, degree in indegree.items() if degree == 0)
    visited: list[str] = []
    while queue:
        node = queue.pop(0)
        visited.append(node)
        for target in sorted(outgoing[node]):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
                queue.sort()
    if len(visited) != len(node_list):
        return False, [f"cycle among: {sorted(set(node_list) - set(visited))}"]
    return True, []


def topological_order(
    nodes: list[str] | None = None,
    edges: list[tuple[str, str]] | None = None,
) -> list[str]:
    ok, errors = topology_is_acyclic(nodes, edges)
    if not ok:
        raise ValueError("; ".join(errors))
    node_list = list(nodes or NODE_ORDER)
    edge_list = list(edges or TOPOLOGY_EDGES)
    indegree = {node: 0 for node in node_list}
    outgoing: dict[str, list[str]] = {node: [] for node in node_list}
    for source, target in edge_list:
        outgoing[source].append(target)
        indegree[target] += 1
    queue = sorted(node for node, degree in indegree.items() if degree == 0)
    result: list[str] = []
    while queue:
        node = queue.pop(0)
        result.append(node)
        for target in sorted(outgoing[node]):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
                queue.sort()
    return result


NODE_COMPUTE: dict[str, Callable[..., dict[str, Any]]] = {
    "quality_clean": quality_clean,
    "near_duplicate": near_duplicate,
    "pair_consistency": pair_consistency,
    "allocation_sampling": allocation_sampling,
}
