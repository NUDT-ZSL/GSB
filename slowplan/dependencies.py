from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


def _stats_node_for_item(item: dict[str, Any]) -> str:
    return "stats:" + item["item_key"]


def node_metadata(node: str) -> dict[str, Any]:
    prefix, _, rest = node.partition(":")
    if prefix == "stats":
        return {"node": node, "node_type": "statistics_item", "key": rest}
    if prefix == "index":
        return {"node": node, "node_type": "index", "index": rest}
    if prefix == "access":
        subtype, _, target = rest.partition(":")
        return {"node": node, "node_type": "access_path", "access_type": subtype, "target": target}
    if prefix == "template":
        return {"node": node, "node_type": "query_template", "template_id": rest}
    if prefix == "profile":
        return {"node": node, "node_type": "profile_conclusion", "template_id": rest}
    return {"node": node, "node_type": "unknown"}


def build_dependency_edges(
    templates: Iterable[dict[str, Any]],
    catalog: Any,
) -> list[dict[str, str]]:
    edges: set[tuple[str, str, str]] = set()
    for table_name, table in catalog.tables.items():
        stats_table = f"stats:table:{table_name}:row_count"
        scan_access = f"access:scan:{table_name}"
        edges.add((stats_table, scan_access, "statistics_to_access_path"))
        for index in table["indexes"]:
            index_name = index["name"]
            stats_index = f"stats:index:{index_name}:prefix_selectivity"
            index_node = f"index:{index_name}"
            index_access = f"access:index:{index_name}"
            edges.add((stats_table, index_node, "statistics_to_index"))
            edges.add((stats_index, index_node, "statistics_to_index"))
            edges.add((index_node, index_access, "index_to_access_path"))
            for column in index["columns"]:
                stats_column = f"stats:column:{table_name}.{column}:ndv"
                edges.add((stats_column, index_node, "statistics_to_index"))
    for template in templates:
        template_id = template["template_id"]
        structure = template["structure"]
        table_name = structure["table"]
        table = catalog.tables[table_name]
        template_node = f"template:{template_id}"
        profile_node = f"profile:{template_id}"
        stats_table = f"stats:table:{table_name}:row_count"
        edges.add((stats_table, template_node, "statistics_to_template"))
        edges.add((f"access:scan:{table_name}", template_node, "access_path_to_template"))
        for predicate in structure["predicates"]:
            column = predicate["column"]
            stats_column = f"stats:column:{table_name}.{column}:ndv"
            edges.add((stats_column, template_node, "statistics_to_template"))
        constrained = {
            predicate["column"]
            for predicate in structure["predicates"]
            if predicate["op"] in {"=", "IN"}
        }
        for index in table["indexes"]:
            if index["columns"] and index["columns"][0] in constrained:
                index_name = index["name"]
                edges.add(
                    (
                        f"access:index:{index_name}",
                        template_node,
                        "access_path_to_template",
                    )
                )
        edges.add((template_node, profile_node, "template_to_profile"))
    return [
        {"src": src, "dst": dst, "edge_type": edge_type}
        for src, dst, edge_type in sorted(edges)
    ]


def _all_paths(
    adjacency: dict[str, list[tuple[str, str]]],
    start: str,
    limit: int = 64,
) -> list[list[dict[str, str]]]:
    paths: list[list[dict[str, str]]] = []
    active: set[str] = set()

    def walk(node: str, path: list[dict[str, str]]) -> None:
        if len(paths) >= limit:
            return
        if node.startswith("profile:"):
            paths.append(list(path))
            return
        active.add(node)
        for dst, edge_type in adjacency.get(node, []):
            if dst in active:
                continue
            path.append({"src": node, "dst": dst, "edge_type": edge_type})
            walk(dst, path)
            path.pop()
        active.remove(node)

    walk(start, [])
    paths.sort(key=lambda path: tuple(edge["dst"] for edge in path))
    return paths


def invalidation_closure(
    changed_items: list[dict[str, Any]],
    edges: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    adjacency: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for edge in edges:
        adjacency[edge["src"]].append((edge["dst"], edge["edge_type"]))
    for node in adjacency:
        adjacency[node] = sorted(set(adjacency[node]))
    affected: dict[str, dict[str, Any]] = {}
    for item in changed_items:
        start = _stats_node_for_item(item)
        for path in _all_paths(adjacency, start):
            if not path:
                continue
            last = path[-1]["dst"]
            if not last.startswith("profile:"):
                continue
            template_id = last.split(":", 1)[1]
            entry = affected.setdefault(
                template_id,
                {
                    "template_id": template_id,
                    "triggers": [],
                    "propagation_paths": [],
                },
            )
            trigger = {
                "stats_item": item["item_key"],
                "kind": item["kind"],
                "table": item["table"],
                "column": item.get("column"),
                "index": item.get("index"),
                "old_value": item.get("old_value"),
                "new_value": item.get("new_value"),
            }
            if trigger not in entry["triggers"]:
                entry["triggers"].append(trigger)
            path_nodes = [start] + [edge["dst"] for edge in path]
            path_payload = {
                "nodes": [node_metadata(node) for node in path_nodes],
                "edges": path,
            }
            if path_payload not in entry["propagation_paths"]:
                entry["propagation_paths"].append(path_payload)
    for entry in affected.values():
        entry["triggers"].sort(key=lambda item: (item["stats_item"], str(item["old_value"]), str(item["new_value"])))
        entry["propagation_paths"].sort(
            key=lambda path: tuple(node["node"] for node in path["nodes"])
        )
    return affected
