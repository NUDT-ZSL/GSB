"""Order-independent dependency graph, invalidation closures and cycle detection."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .codes import E_BIND_CYCLE


def user_node(user_id: str) -> str:
    return f"user:{user_id}"


def device_node(device_id: str) -> str:
    return f"device:{device_id}"


def factor_node(factor_id: str) -> str:
    return f"factor:{factor_id}"


def session_node(session_id: str) -> str:
    return f"session:{session_id}"


def binding_node(binding_id: str) -> str:
    return f"binding:{binding_id}"


def build_dependency_graph(bundle: Any) -> dict[str, Any]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    reverse: dict[str, set[str]] = defaultdict(set)
    edges: list[dict[str, str]] = []
    nodes: set[str] = set()

    def add_edge(source: str, target: str, edge_type: str) -> None:
        if not source or not target:
            return
        nodes.add(source)
        nodes.add(target)
        if target not in adjacency[source]:
            adjacency[source].add(target)
            reverse[target].add(source)
            edges.append({"from_node": source, "to_node": target, "edge_type": edge_type})

    for device in bundle.devices:
        add_edge(user_node(device["user_id"]), device_node(device["device_id"]), "USER_DEVICE")

    for factor in bundle.factors:
        if factor.get("device_id"):
            add_edge(device_node(factor["device_id"]), factor_node(factor["factor_id"]), "DEVICE_FACTOR")
        else:
            add_edge(user_node(factor["user_id"]), factor_node(factor["factor_id"]), "USER_FACTOR")

    for session in bundle.sessions:
        if session.get("device_id") and not session.get("factor_ids"):
            add_edge(device_node(session["device_id"]), session_node(session["session_id"]), "DEVICE_SESSION")
        for factor_id in session.get("factor_ids", []):
            add_edge(factor_node(factor_id), session_node(session["session_id"]), "FACTOR_SESSION")

    for binding in getattr(bundle, "_bindings", []) or []:
        add_edge(binding_node(binding["binding_id"]), device_node(binding["device_id"]), "BINDING_DEVICE")

    for edge in bundle.edges:
        add_edge(edge["from_node"], edge["to_node"], edge.get("edge_type", "GENERIC"))

    for record in bundle.users:
        nodes.add(user_node(record["user_id"]))
    for record in bundle.devices:
        nodes.add(device_node(record["device_id"]))
    for record in bundle.factors:
        nodes.add(factor_node(record["factor_id"]))
    for record in bundle.sessions:
        nodes.add(session_node(record["session_id"]))

    return {
        "nodes": sorted(nodes),
        "adjacency": {key: sorted(value) for key, value in sorted(adjacency.items())},
        "reverse": {key: sorted(value) for key, value in sorted(reverse.items())},
        "edges": sorted(edges, key=lambda e: (e["from_node"], e["to_node"], e["edge_type"])),
    }


def reachable_closure(graph: dict[str, Any], roots: Iterable[str]) -> set[str]:
    adjacency = graph.get("adjacency", {})
    visited: set[str] = set()
    stack = sorted(set(roots))
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        for neighbor in adjacency.get(node, []):
            if neighbor not in visited:
                stack.append(neighbor)
    return visited


def closure_by_roots(graph: dict[str, Any], roots: list[dict[str, Any]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for root in roots:
        node = root["node"]
        result[node] = sorted(reachable_closure(graph, [node]))
    return dict(sorted(result.items()))


def find_cycles(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Return deterministic SCC-based cycle descriptors."""

    nodes = list(graph.get("nodes", []))
    adjacency = graph.get("adjacency", {})
    reverse = graph.get("reverse", {})

    visited: set[str] = set()
    finish_order: list[str] = []
    for start in nodes:
        if start in visited:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        visited.add(start)
        while stack:
            node, next_index = stack[-1]
            neighbors = adjacency.get(node, [])
            if next_index < len(neighbors):
                neighbor = neighbors[next_index]
                stack[-1] = (node, next_index + 1)
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append((neighbor, 0))
            else:
                finish_order.append(node)
                stack.pop()

    assigned: set[str] = set()
    components: list[list[str]] = []
    for start in reversed(finish_order):
        if start in assigned:
            continue
        component: list[str] = []
        stack = [start]
        assigned.add(start)
        while stack:
            node = stack.pop()
            component.append(node)
            for neighbor in reverse.get(node, []):
                if neighbor not in assigned:
                    assigned.add(neighbor)
                    stack.append(neighbor)
        components.append(sorted(component))

    edge_set = {
        (edge["from_node"], edge["to_node"]): edge.get("edge_type", "GENERIC")
        for edge in graph.get("edges", [])
    }
    cycles: list[dict[str, Any]] = []
    for component in sorted(components, key=lambda item: tuple(item)):
        node_set = set(component)
        has_self_loop = any((node, node) in edge_set for node in component)
        if len(component) == 1 and not has_self_loop:
            continue
        edge_types = sorted(
            edge_type
            for (source, target), edge_type in edge_set.items()
            if source in node_set and target in node_set
        )
        if "FACTOR_DEPENDENCY" in edge_types:
            cycle_type = "FACTOR_DEPENDENCY_CYCLE"
        elif "DEVICE_BINDING_CHAIN" in edge_types or any(node.startswith("binding:") for node in component):
            cycle_type = "DEVICE_BINDING_CHAIN_CYCLE"
        elif any(node.startswith("factor:") for node in component):
            cycle_type = "FACTOR_DEPENDENCY_CYCLE"
        else:
            cycle_type = "BIND_DEPENDENCY_CYCLE"
        path = _canonical_cycle_path(adjacency, component)
        cycles.append(
            {
                "code": E_BIND_CYCLE,
                "cycle_type": cycle_type,
                "nodes": component,
                "canonical_cycle": path,
                "edge_types": edge_types,
            }
        )
    return sorted(cycles, key=lambda cycle: (cycle["cycle_type"], tuple(cycle["nodes"])))


def _canonical_cycle_path(adjacency: dict[str, list[str]], component: list[str]) -> list[str]:
    node_set = set(component)
    start = component[0]
    path = [start]
    current = start
    seen_edges: set[tuple[str, str]] = set()
    for _ in range(len(component) + 2):
        candidates = [
            neighbor
            for neighbor in adjacency.get(current, [])
            if neighbor in node_set and (current, neighbor) not in seen_edges
        ]
        if not candidates:
            break
        nxt = candidates[0]
        seen_edges.add((current, nxt))
        path.append(nxt)
        if nxt == start:
            return path
        current = nxt
    return component + [component[0]]


def revocation_roots(
    bundle: Any,
    view: dict[str, Any] | None = None,
    at_ms: int | None = None,
    watermark: int | None = None,
) -> list[dict[str, str]]:
    roots: dict[str, dict[str, str]] = {}

    def add(node: str, reason: str, event_id: str) -> None:
        if node:
            roots[node] = {"node": node, "reason": reason, "source_event": event_id}

    for factor in bundle.factors:
        if factor.get("status") in {"UNBOUND", "TOMBSTONED"}:
            add(factor_node(factor["factor_id"]), f"factor_{factor['status'].lower()}", f"declared:{factor['factor_id']}")

    if view:
        for binding in view.get("bindings", []):
            if binding.get("tombstoned"):
                if at_ms is not None and binding["effective_at_ms"] > at_ms:
                    continue
                if watermark is not None and binding.get("batch_watermark", 1) > watermark:
                    continue
                add(binding_node(binding["binding_id"]), "binding_tombstoned", f"v2:{binding['binding_id']}")
                add(device_node(binding["device_id"]), "binding_tombstoned", f"v2:{binding['binding_id']}")

    for event in bundle.revocations:
        if at_ms is not None and event["effective_at_ms"] > at_ms:
            continue
        if watermark is not None and event["batch_watermark"] > watermark:
            continue
        event_id = event["event_id"]
        event_type = event["event_type"]
        if event_type == "FACTOR_UNBIND":
            factor_id = event.get("factor_id") or event.get("target_id")
            add(factor_node(factor_id), "factor_unbound", event_id)
        elif event_type == "DEVICE_BINDING_TOMBSTONE":
            if event.get("device_id"):
                add(device_node(event["device_id"]), "device_binding_tombstoned", event_id)
            if event.get("binding_id"):
                add(binding_node(event["binding_id"]), "device_binding_tombstoned", event_id)
        elif event_type == "CREDENTIAL_MIGRATION":
            targets = event.get("target_user_ids")
            if isinstance(targets, list):
                for user_id in targets:
                    add(user_node(user_id), "credential_migration", event_id)
            else:
                user_id = event.get("user_id") or event.get("target_id")
                add(user_node(user_id), "credential_migration", event_id)
        elif event_type == "POLICY_UPGRADE":
            required_tier = event.get("new_min_digest_tier")
            required_iterations = event.get("new_min_iterations")
            for user in bundle.users:
                if (
                    (isinstance(required_tier, int) and user["digest_tier"] < required_tier)
                    or
                    (isinstance(required_iterations, int) and user["iterations"] < required_iterations)
                ):
                    add(user_node(user["user_id"]), "policy_upgrade_noncompliant", event_id)

    return [roots[key] for key in sorted(roots)]


def build_closure_report(
    bundle: Any,
    view: dict[str, Any],
    watermark: int | None = None,
    at_ms: int | None = None,
) -> dict[str, Any]:
    if not hasattr(bundle, "_bindings"):
        bundle._bindings = view.get("bindings", [])
    graph = build_dependency_graph(bundle)
    roots = revocation_roots(bundle, view=view, at_ms=at_ms, watermark=watermark)
    closure = reachable_closure(graph, [root["node"] for root in roots])
    cycles = find_cycles(graph)
    by_root = closure_by_roots(graph, roots)

    attempt_hits: list[dict[str, Any]] = []
    for attempt in sorted(bundle.attempts, key=lambda a: (a["user_id"], a["attempt_seq"])):
        if not attempt.get("_valid", True):
            continue
        dependencies = attempt_dependency_nodes(attempt, bundle)
        hit = sorted(node for node in dependencies if node in closure)
        cycle_hit = sorted(
            cycle for cycle in cycles
            if any(node in set(cycle["nodes"]) for node in dependencies)
        )
        if hit or cycle_hit:
            attempt_hits.append(
                {
                    "user_id": attempt["user_id"],
                    "attempt_seq": attempt["attempt_seq"],
                    "invalidated_dependency_nodes": hit,
                    "cycles": cycle_hit,
                }
            )

    return {
        "watermark": watermark,
        "at_ms": at_ms,
        "roots": roots,
        "closure_nodes": sorted(closure),
        "invalidated_sessions": sorted(node for node in closure if node.startswith("session:")),
        "invalidated_factors": sorted(node for node in closure if node.startswith("factor:")),
        "invalidated_devices": sorted(node for node in closure if node.startswith("device:") or node.startswith("binding:")),
        "closure_by_root": by_root,
        "cycles": cycles,
        "attempt_hits": attempt_hits,
        "graph_node_count": len(graph["nodes"]),
        "graph_edge_count": len(graph["edges"]),
    }


def attempt_dependency_nodes(attempt: dict[str, Any], bundle: Any) -> list[str]:
    nodes = [user_node(attempt["user_id"])]
    if attempt.get("device_id"):
        nodes.append(device_node(attempt["device_id"]))
    if attempt.get("session_id"):
        nodes.append(session_node(attempt["session_id"]))
    for factor in attempt.get("factors", []):
        factor_id = factor.get("factor_id") if isinstance(factor, dict) else None
        if factor_id:
            nodes.append(factor_node(factor_id))
    return sorted(set(nodes))
