from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .codes import E_ENTITY_NOT_FOUND, E_TOKEN_CYCLE, OK
from .io_utils import read_json_file
from .merge import MergedView
from .model import code_key, record_entity_key, session_key, token_key


JsonDict = dict[str, Any]


@dataclass
class RevocationRequest:
    request_id: str
    entity_type: str
    entity_id: str
    entity_key: Optional[str]
    client_id: Optional[str]
    source: JsonDict

    @classmethod
    def from_raw(cls, raw: JsonDict, path: str, index: int) -> "RevocationRequest":
        entity_type = str(raw.get("entity_type") or raw.get("type") or "").strip()
        entity_id = str(raw.get("entity_id") or raw.get("id") or "").strip()
        return cls(
            request_id=str(raw.get("request_id") or raw.get("id") or f"revocation-{index}"),
            entity_type=entity_type,
            entity_id=entity_id,
            entity_key=raw.get("entity_key"),
            client_id=raw.get("client_id"),
            source={
                "source_version": "revocation-input",
                "path": path.replace("\\", "/"),
                "record_no": index,
                "record_index": index - 1,
                "record_id": str(raw.get("request_id") or raw.get("id") or f"revocation-{index}"),
            },
        )

    def resolved_key(self) -> Optional[str]:
        if self.entity_key:
            return str(self.entity_key)
        kind = self.entity_type.lower()
        if kind == "session":
            return session_key(self.entity_id)
        if kind in {"code", "authorization_code"}:
            return code_key(self.client_id or "", self.entity_id)
        if kind == "access_token":
            return token_key(self.entity_id, "access_token")
        if kind == "refresh_token":
            return token_key(self.entity_id, "refresh_token")
        return None


def load_revocations(path: str | Path) -> list[RevocationRequest]:
    p = Path(path)
    data = read_json_file(p)
    raw_requests = data.get("revocations", []) if isinstance(data, dict) else data
    if not isinstance(raw_requests, list):
        raise ValueError("revocation input must be a list or contain a revocations array")
    display = str(p).replace("\\", "/")
    return [
        RevocationRequest.from_raw(raw, display, index)
        for index, raw in enumerate(raw_requests, start=1)
        if isinstance(raw, dict)
    ]


@dataclass
class DependencyGraph:
    nodes: dict[str, JsonDict] = field(default_factory=dict)
    adjacency: dict[str, set[str]] = field(default_factory=dict)
    edges: list[JsonDict] = field(default_factory=list)

    def add_node(self, key: str, node_type: str, **metadata: Any) -> None:
        existing = self.nodes.get(key)
        if existing is None:
            self.nodes[key] = {"entity_key": key, "type": node_type, **metadata}
            self.adjacency[key] = set()
        else:
            existing.update({k: v for k, v in metadata.items() if v is not None})

    def add_edge(self, source: str, target: str, relation: str) -> None:
        if source == target:
            return
        self.add_node(source, source.split(":", 1)[0])
        self.add_node(target, target.split(":", 1)[0])
        if target not in self.adjacency[source]:
            self.adjacency[source].add(target)
            self.edges.append({"from": source, "to": target, "relation": relation})

    def sorted_adjacency(self, node: str) -> list[str]:
        return sorted(self.adjacency.get(node, set()))


def build_graph(view: MergedView) -> DependencyGraph:
    graph = DependencyGraph()

    for key, entity in view.entities.items():
        authoritative = entity.authoritative
        graph.add_node(
            key,
            entity.entity_type,
            status=entity.status,
            tombstoned=entity.status == "tombstoned",
            generation=authoritative.generation if authoritative else None,
            source=authoritative.source_dict() if authoritative else None,
        )

    for entity in view.entities.values():
        for record in entity.records:
            key = record_entity_key(record)
            if record.session_id:
                session = session_key(record.session_id)
                graph.add_node(session, "session")
                if record.record_type == "session":
                    pass
                elif record.record_type == "authorization_code":
                    graph.add_edge(session, key, "session_to_authorization_code")
                elif record.record_type == "access_token":
                    graph.add_edge(session, key, "session_to_access_token")

            if record.record_type == "authorization_code":
                continue

            code_entity = None
            if record.code and record.client_id:
                code_entity = code_key(record.client_id, record.code)
            elif record.code:
                candidates = view.code_index().get(record.code, [])
                if len(candidates) == 1:
                    code_entity = candidates[0]

            if record.record_type == "access_token":
                access_key = key
                if code_entity:
                    graph.add_edge(code_entity, access_key, "authorization_code_to_access_token")
                if record.refresh_token_id:
                    refresh_key = token_key(record.refresh_token_id, "refresh_token")
                    graph.add_edge(access_key, refresh_key, "access_token_to_refresh_token")

            if record.record_type == "refresh_token":
                refresh_key = key
                if record.access_token_id:
                    access_key = token_key(record.access_token_id, "access_token")
                    graph.add_edge(access_key, refresh_key, "access_token_to_refresh_token")
                if record.successor_id:
                    successor = token_key(record.successor_id, "refresh_token")
                    graph.add_edge(refresh_key, successor, "refresh_rotation_successor")
                if record.rotates_from_id:
                    predecessor = token_key(record.rotates_from_id, "refresh_token")
                    graph.add_edge(predecessor, refresh_key, "refresh_rotation_successor")

    chains: dict[str, list[Any]] = {}
    for entity in view.entities.values():
        for record in entity.records:
            if record.record_type == "refresh_token" and record.chain_id:
                chains.setdefault(record.chain_id, []).append(record)

    for chain_id, records in chains.items():
        ordered = sorted(records, key=lambda item: (item.generation, item.as_of_ms))
        for previous, current in zip(ordered, ordered[1:]):
            previous_key = record_entity_key(previous)
            current_key = record_entity_key(current)
            if previous.generation < current.generation:
                graph.add_edge(previous_key, current_key, f"refresh_generation_chain:{chain_id}")

    graph.edges.sort(key=lambda edge: (edge["from"], edge["to"], edge["relation"]))
    return graph


def closure_from_roots(
    graph: DependencyGraph, roots: list[str]
) -> tuple[set[str], dict[str, list[str]], list[list[str]]]:
    ordered_roots = sorted(set(roots))
    closure: set[str] = set()
    paths: dict[str, list[str]] = {}
    queue: deque[tuple[str, list[str]]] = deque()

    for root in ordered_roots:
        if root not in closure:
            closure.add(root)
            paths[root] = [root]
            queue.append((root, [root]))

    while queue:
        node, path = queue.popleft()
        for nxt in graph.sorted_adjacency(node):
            next_path = path + [nxt]
            if nxt not in closure:
                closure.add(nxt)
                paths[nxt] = next_path
                queue.append((nxt, next_path))
            elif nxt not in paths:
                paths[nxt] = next_path

    cycles = find_reachable_cycles(graph, ordered_roots)
    return closure, paths, cycles


def find_reachable_cycles(graph: DependencyGraph, roots: list[str]) -> list[list[str]]:
    cycles: list[list[str]] = []
    color: dict[str, int] = {node: 0 for node in graph.nodes}
    stack: list[str] = []

    def dfs(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in graph.sorted_adjacency(node):
            if nxt not in color:
                color[nxt] = 0
            if color[nxt] == 0:
                dfs(nxt)
            elif color[nxt] == 1 and nxt in stack:
                index = stack.index(nxt)
                cycle = stack[index:] + [nxt]
                if cycle not in cycles:
                    cycles.append(cycle)
        stack.pop()
        color[node] = 2

    for root in sorted(roots):
        if root in graph.nodes and color.get(root, 0) == 0:
            dfs(root)
    cycles.sort(key=lambda cycle: "->".join(cycle))
    return cycles


def propagate_requests(
    view: MergedView, requests: list[RevocationRequest]
) -> JsonDict:
    graph = build_graph(view)
    individual: list[JsonDict] = []
    valid_roots: list[str] = []
    missing: list[JsonDict] = []

    for request in requests:
        root = request.resolved_key()
        if not root or root not in graph.nodes:
            result = {
                "request_id": request.request_id,
                "primary_code": E_ENTITY_NOT_FOUND,
                "business_code": E_ENTITY_NOT_FOUND,
                "verdict": "denied",
                "root": root,
                "source": request.source,
                "closure": [],
                "cycles": [],
            }
            individual.append(result)
            missing.append(result)
            continue

        valid_roots.append(root)
        closure, paths, cycles = closure_from_roots(graph, [root])
        individual.append(
            _result_dict(request.request_id, root, request.source, closure, paths, cycles, graph)
        )

    combined_closure, combined_paths, combined_cycles = closure_from_roots(graph, valid_roots)
    combined = _closure_details(graph, combined_closure, combined_paths)
    active_nodes = sorted(
        key for key, node in graph.nodes.items() if node.get("status") == "active"
    )
    tombstoned_nodes = sorted(
        key for key, node in graph.nodes.items() if node.get("tombstoned")
    )
    conflict_nodes = sorted(
        key for key, node in graph.nodes.items() if node.get("status") == "conflict"
    )
    remaining_valid = sorted(set(active_nodes) - combined_closure)
    codes = [item["primary_code"] for item in individual]
    if combined_cycles:
        combined_code = E_TOKEN_CYCLE
    elif any(code == E_ENTITY_NOT_FOUND for code in codes):
        combined_code = E_ENTITY_NOT_FOUND
    else:
        combined_code = OK

    return {
        "adjudication": "revocation_propagation",
        "primary_code": combined_code,
        "business_code": combined_code,
        "order_independence": {
            "root_order_used": sorted(set(valid_roots)),
            "note": "roots are sorted before BFS; sorted closure is input-order independent",
        },
        "graph": {
            "node_count": len(graph.nodes),
            "edge_count": len(graph.edges),
            "edges": graph.edges,
        },
        "active_nodes": active_nodes,
        "tombstoned_nodes": tombstoned_nodes,
        "conflict_nodes": conflict_nodes,
        "effective_valid_nodes_before_revocation": active_nodes,
        "combined_closure": combined,
        "combined_closure_keys": sorted(combined_closure),
        "cycles": combined_cycles,
        "remaining_valid_nodes": remaining_valid,
        "requests": individual,
        "summary": {
            "total_requests": len(requests),
            "resolved_roots": len(set(valid_roots)),
            "missing_roots": len(missing),
            "closure_size": len(combined_closure),
            "cycle_count": len(combined_cycles),
            "all_succeeded": combined_code == OK,
        },
    }


def _result_dict(
    request_id: str,
    root: str,
    source: JsonDict,
    closure: set[str],
    paths: dict[str, list[str]],
    cycles: list[list[str]],
    graph: DependencyGraph,
) -> JsonDict:
    primary = E_TOKEN_CYCLE if cycles else OK
    return {
        "request_id": request_id,
        "root": root,
        "source": source,
        "primary_code": primary,
        "business_code": primary,
        "verdict": "computed" if primary == OK else "cycle_detected",
        "closure": _closure_details(graph, closure, paths),
        "closure_keys": sorted(closure),
        "cycles": cycles,
    }


def _closure_details(
    graph: DependencyGraph, closure: set[str], paths: dict[str, list[str]]
) -> list[JsonDict]:
    details: list[JsonDict] = []
    for key in sorted(closure):
        node = graph.nodes.get(key, {"entity_key": key, "type": "unknown"})
        path = paths.get(key, [key])
        details.append(
            {
                "entity_key": key,
                "type": node.get("type"),
                "status": node.get("status", "unknown"),
                "tombstoned": bool(node.get("tombstoned")),
                "depth": max(len(path) - 1, 0),
                "path": path,
                "source": node.get("source"),
            }
        )
    return details
