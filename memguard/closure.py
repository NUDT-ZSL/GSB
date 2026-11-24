"""Ownership closures and dependency-cycle detection for credentials."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


RegionKey = Tuple[str, str, str, int]


def _cred_region_key(cred: Dict[str, Any]) -> Tuple[str, str, str]:
    return (
        cred["endpoint_id"],
        cred["process_guid"],
        cred["region_id"],
    )


def build_dependency_graph(
    credentials: Iterable[Dict[str, Any]],
    regions: Iterable[Dict[str, Any]],
) -> Dict[str, Set[str]]:
    creds = list(credentials)
    graph: Dict[str, Set[str]] = {cred["credential_id"]: set() for cred in creds}

    region_decl = {
        (r["endpoint_id"], r["process_guid"], r["region_id"]): r for r in regions
    }
    creds_by_region: Dict[Tuple[str, str, str], List[str]] = defaultdict(list)
    for cred in creds:
        creds_by_region[_cred_region_key(cred)].append(cred["credential_id"])

    for cred in creds:
        cid = cred["credential_id"]
        for dep_id in cred.get("depends_on_credentials", []) or []:
            if dep_id in graph:
                graph[cid].add(dep_id)

        region = region_decl.get(_cred_region_key(cred))
        if not region:
            continue
        for dep_region_id in region.get("rollback_depends_on_regions", []) or []:
            dep_key = (
                region["endpoint_id"],
                region["process_guid"],
                dep_region_id,
            )
            for dep_cred_id in creds_by_region.get(dep_key, []):
                if dep_cred_id != cid:
                    graph[cid].add(dep_cred_id)
    return graph


def find_cycles(graph: Dict[str, Set[str]]) -> List[List[str]]:
    """Return deterministic strongly-connected components containing cycles."""
    nodes = sorted(graph)
    index = 0
    indices: Dict[str, int] = {}
    low: Dict[str, int] = {}
    stack: List[str] = []
    on_stack: Set[str] = set()
    sccs: List[List[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal index
        indices[node] = index
        low[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in sorted(graph.get(node, ())):
            if neighbor not in indices:
                strongconnect(neighbor)
                low[node] = min(low[node], low[neighbor])
            elif neighbor in on_stack:
                low[node] = min(low[node], indices[neighbor])

        if low[node] == indices[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1 or node in graph.get(node, set()):
                sccs.append(sorted(component))

    for node in nodes:
        if node not in indices:
            strongconnect(node)
    return sorted(sccs, key=lambda component: component[0])


def topological_order(
    credential_ids: Iterable[str], graph: Dict[str, Set[str]]
) -> List[str]:
    ids = set(credential_ids)
    indegree = {cid: 0 for cid in ids}
    for cid in ids:
        for dep in graph.get(cid, set()):
            if dep in ids:
                indegree[dep] = indegree.get(dep, 0) + 1

    ready = sorted(cid for cid, degree in indegree.items() if degree == 0)
    order: List[str] = []
    while ready:
        cid = ready.pop(0)
        order.append(cid)
        for dep in sorted(graph.get(cid, set())):
            if dep not in ids:
                continue
            indegree[dep] -= 1
            if indegree[dep] == 0:
                ready.append(dep)
                ready.sort()
    if len(order) != len(ids):
        remaining = sorted(ids - set(order))
        raise ValueError(f"dependency cycle prevents ordering: {remaining}")
    return order


def _module_key(cred: Dict[str, Any]) -> Tuple[str, str, str]:
    return (cred["endpoint_id"], cred["process_guid"], cred.get("module", ""))


def _process_key(cred: Dict[str, Any]) -> Tuple[str, str]:
    return (cred["endpoint_id"], cred["process_guid"])


def group_ownership_closures(
    credentials: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    creds = sorted(
        credentials,
        key=lambda c: (
            c["endpoint_id"],
            c["process_guid"],
            c.get("module", ""),
            c["region_id"],
            c["credential_id"],
        ),
    )
    modules: Dict[Tuple[str, str, str], List[str]] = defaultdict(list)
    processes: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for cred in creds:
        modules[_module_key(cred)].append(cred["credential_id"])
        processes[_process_key(cred)].append(cred["credential_id"])

    module_closures = []
    process_closures = []
    module_index: Dict[str, str] = {}
    process_index: Dict[str, str] = {}

    for key, ids in sorted(modules.items()):
        endpoint, process_guid, module = key
        closure_id = "closure-module-" + "-".join(
            part.replace(":", "_").replace("{", "").replace("}", "")[:24]
            for part in key
        )
        module_closures.append(
            {
                "closure_id": closure_id,
                "scope": "module",
                "endpoint_id": endpoint,
                "process_guid": process_guid,
                "module": module,
                "credential_ids": sorted(ids),
            }
        )
        for cid in ids:
            module_index[cid] = closure_id

    for key, ids in sorted(processes.items()):
        endpoint, process_guid = key
        closure_id = "closure-process-" + "-".join(
            part.replace(":", "_").replace("{", "").replace("}", "")[:32]
            for part in key
        )
        process_closures.append(
            {
                "closure_id": closure_id,
                "scope": "process",
                "endpoint_id": endpoint,
                "process_guid": process_guid,
                "credential_ids": sorted(ids),
            }
        )
        for cid in ids:
            process_index[cid] = closure_id

    return {
        "module_closures": module_closures,
        "process_closures": process_closures,
        "module_closure_by_credential": module_index,
        "process_closure_by_credential": process_index,
    }


def closure_for_credential(
    root_credential_id: str,
    credentials: Iterable[Dict[str, Any]],
    regions: Iterable[Dict[str, Any]],
    *,
    scope: str = "module",
) -> Dict[str, Any]:
    creds = list(credentials)
    by_id = {cred["credential_id"]: cred for cred in creds}
    if root_credential_id not in by_id:
        return {
            "status": "not_found",
            "business_code": "E_PARAM",
            "root_credential_id": root_credential_id,
        }

    graph = build_dependency_graph(creds, regions)
    root = by_id[root_credential_id]
    selected: Set[str] = {root_credential_id}
    if scope == "module":
        key = _module_key(root)
        selected.update(
            cid for cid, cred in by_id.items() if _module_key(cred) == key
        )
    elif scope == "process":
        key = _process_key(root)
        selected.update(
            cid for cid, cred in by_id.items() if _process_key(cred) == key
        )
    elif scope == "region":
        pass
    else:
        return {
            "status": "invalid_scope",
            "business_code": "E_PARAM",
            "scope": scope,
        }

    queue = list(selected)
    while queue:
        cid = queue.pop()
        for dep in graph.get(cid, set()):
            if dep in by_id and dep not in selected:
                selected.add(dep)
                queue.append(dep)

    ids = sorted(selected)
    subgraph = {cid: graph.get(cid, set()) & selected for cid in ids}
    cycles = find_cycles(subgraph)
    if cycles:
        return {
            "status": "cycle",
            "business_code": "E_CRED_CYCLE",
            "root_credential_id": root_credential_id,
            "cycles": cycles,
        }

    order = topological_order(ids, graph)
    return {
        "status": "ok",
        "business_code": "E_OK",
        "root_credential_id": root_credential_id,
        "scope": scope,
        "credential_ids": ids,
        "rollback_order": order,
        "dependency_edges": {
            cid: sorted(subgraph.get(cid, set())) for cid in ids
        },
    }


def full_closure_report(
    credentials: Iterable[Dict[str, Any]],
    regions: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    creds = list(credentials)
    graph = build_dependency_graph(creds, regions)
    cycles = find_cycles(graph)
    ownership = group_ownership_closures(creds)
    return {
        "status": "cycle" if cycles else "ok",
        "business_code": "E_CRED_CYCLE" if cycles else "E_OK",
        "cycles": cycles,
        "dependency_graph": {
            cid: sorted(deps) for cid, deps in sorted(graph.items())
        },
        **ownership,
    }
