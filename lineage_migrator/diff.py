"""Snapshot-to-snapshot lineage diff with rule attribution.

The report is computed exclusively from committed snapshot tables, so it can
never contain an edge absent from either snapshot.
"""

from __future__ import annotations

from .models import Edge
from .store import Store


def _edge_endpoints(edge: Edge) -> tuple[str, str]:
    return edge.source_ref, edge.target_ref


def _touches_filter(edge: Edge, table: str | None, field_ref: str | None) -> bool:
    if field_ref is not None:
        return edge.source_ref == field_ref or edge.target_ref == field_ref
    if table is not None:
        endpoints = (edge.source_ref, edge.target_ref)
        return any(endpoint.startswith(f"{table}.") for endpoint in endpoints)
    return True


def diff_versions(
    store: Store,
    from_version: int,
    to_version: int,
    *,
    table: str | None = None,
    field_ref: str | None = None,
) -> dict:
    if store.get_version(from_version) is None:
        raise ValueError(f"version {from_version} has no snapshot")
    if store.get_version(to_version) is None:
        raise ValueError(f"version {to_version} has no snapshot")

    old_edges = {edge.edge_id: edge for edge in store.snapshot_edges(from_version)}
    new_edges = {edge.edge_id: edge for edge in store.snapshot_edges(to_version)}
    changes = store.change_log()

    added, removed, endpoint_changed = [], [], []
    for edge_id in sorted(set(new_edges) - set(old_edges)):
        edge = new_edges[edge_id]
        if _touches_filter(edge, table, field_ref):
            added.append(_edge_payload(edge, changes))
    for edge_id in sorted(set(old_edges) - set(new_edges)):
        edge = old_edges[edge_id]
        if _touches_filter(edge, table, field_ref):
            removed.append(_edge_payload(edge, changes))
    for edge_id in sorted(set(old_edges) & set(new_edges)):
        before, after = old_edges[edge_id], new_edges[edge_id]
        if _edge_endpoints(before) != _edge_endpoints(after) and _touches_filter(after, table, field_ref):
            endpoint_changed.append({
                "edge_id": edge_id,
                "before": {"source_ref": before.source_ref, "target_ref": before.target_ref},
                "after": {"source_ref": after.source_ref, "target_ref": after.target_ref},
                "attribution": _attribute_edge(edge_id, changes),
            })

    return {
        "from_version": from_version,
        "to_version": to_version,
        "filter": {"table": table, "field_ref": field_ref},
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "endpoint_changed": len(endpoint_changed),
        },
        "added": added,
        "removed": removed,
        "endpoint_changed": endpoint_changed,
    }


def _edge_payload(edge: Edge, changes: list[dict]) -> dict:
    return {
        "edge_id": edge.edge_id,
        "source_ref": edge.source_ref,
        "target_ref": edge.target_ref,
        "origin": edge.origin,
        "attribution": _attribute_edge(edge.edge_id, changes),
    }


def _attribute_edge(edge_id: str, changes: list[dict]) -> list[dict]:
    """Trace an edge back to the rule and version that created/rewrote it.

    Split/merge edges encode their parent edge id in ``origin``; the search
    walks both direct id mentions and origin-derived parent ids.
    """
    hits = []
    for change in changes:
        detail = change["detail"]
        mentioned = set()
        for key in ("new_edges", "replaced_edges", "rewritten_edges", "deleted_edge_ids"):
            value = detail.get(key)
            if isinstance(value, list):
                mentioned.update(value)
        if edge_id in mentioned:
            hits.append({
                "version": change["version"],
                "rule_id": change["rule_id"],
                "rule_type": change["rule_type"],
                "action": change["action"],
            })
    return hits
