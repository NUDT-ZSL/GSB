from __future__ import annotations

from collections import deque
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .model import operation_target
from .sqlparse import parse_query_dependencies


def _node_kind(node: str) -> str:
    return node.split(":", 1)[0]


def _node_name(node: str) -> str:
    return node.split(":", 1)[1]


def operation_summaries(migration: Dict[str, Any]) -> List[Dict[str, Any]]:
    summaries = []
    for index, operation in enumerate(migration["operations"]):
        summaries.append(
            {
                "op_id": f"{migration['id']}.{index + 1:03d}",
                "op": operation["op"],
                "target": operation_target(operation),
                "index": index,
            }
        )
    return summaries


class DependencyGraph:
    def __init__(self) -> None:
        self.nodes: Set[str] = set()
        self.edges: Dict[str, Set[str]] = {}

    def add_node(self, node: str) -> None:
        self.nodes.add(node)
        self.edges.setdefault(node, set())

    def add_edge(self, dependency: str, dependent: str) -> None:
        self.add_node(dependency)
        self.add_node(dependent)
        self.edges[dependency].add(dependent)

    def reachable(self, roots: Iterable[str]) -> Dict[str, Tuple[str, List[str]]]:
        found: Dict[str, Tuple[str, List[str]]] = {}
        queue = deque()
        for root in sorted(set(roots)):
            self.add_node(root)
            queue.append((root, [root]))
        while queue:
            node, path = queue.popleft()
            existing = found.get(node)
            if existing is not None:
                continue
            found[node] = (path[0], path)
            for child in sorted(self.edges.get(node, set())):
                if child not in found:
                    queue.append((child, path + [child]))
        return found


def _add_references(
    graph: DependencyGraph,
    schema: Dict[str, Any],
    references: Iterable[str],
    dependent: str,
) -> None:
    graph.add_node(dependent)
    for reference in sorted(set(references)):
        graph.add_edge(reference, dependent)
    kind, name = dependent.split(":", 1)
    if kind == "view" and f"table:{name}" in graph.nodes:
        return


def _schema_graph_edges(graph: DependencyGraph, schema: Dict[str, Any]) -> None:
    for table in schema.get("tables", []):
        graph.add_node(f"table:{table['name']}")
        for column in table.get("columns", []):
            graph.add_node(f"column:{table['name']}.{column['name']}")
    for index in schema.get("indexes", []):
        dependencies = [f"table:{index['table']}"]
        dependencies.extend(f"column:{index['table']}.{column}" for column in index["columns"])
        _add_references(graph, schema, dependencies, f"index:{index['name']}")
    for view in schema.get("views", []):
        dependencies = list(view.get("references", []))
        try:
            parsed = parse_query_dependencies(view["sql"], schema, f"view:{view['name']}")
            dependencies.extend(parsed["references"])
        except Exception:
            # Validation has already made view SQL deterministic. If a schema
            # exists at one side of a migration before a column was added, the
            # other side still contributes the needed edges.
            pass
        _add_references(graph, schema, dependencies, f"view:{view['name']}")
    for trigger in schema.get("triggers", []):
        dependencies = [f"table:{trigger['table']}"]
        dependencies.extend(trigger.get("references", []))
        _add_references(graph, schema, dependencies, f"trigger:{trigger['name']}")


def _template_dependencies(
    template: Dict[str, Any],
    base_schema: Dict[str, Any],
    target_schema: Dict[str, Any],
) -> List[str]:
    references = set(template.get("references", []))
    parsed_any = False
    for schema in (base_schema, target_schema):
        try:
            parsed = parse_query_dependencies(template["sql"], schema, f"template:{template['id']}")
            references.update(parsed["references"])
            parsed_any = True
        except Exception:
            continue
    if not parsed_any:
        references.update(template.get("_derived_references", []))
    return sorted(references)


def build_dependency_graph(
    base_schema: Dict[str, Any],
    target_schema: Dict[str, Any],
    templates: Dict[str, Dict[str, Any]],
) -> DependencyGraph:
    graph = DependencyGraph()
    _schema_graph_edges(graph, base_schema)
    _schema_graph_edges(graph, target_schema)
    for template_id, template in sorted(templates.items()):
        node = f"template:{template_id}"
        for dependency in _template_dependencies(template, base_schema, target_schema):
            graph.add_edge(dependency, node)
    return graph


def _operation_roots(operation: Dict[str, Any]) -> List[str]:
    target = operation_target(operation)
    if operation["op"] == "rename_table":
        return [f"table:{operation['old_name']}", f"table:{operation['new_name']}"]
    if operation["op"] == "rename_column":
        return [
            f"column:{operation['table']}.{operation['old_name']}",
            f"column:{operation['table']}.{operation['new_name']}",
        ]
    return [target]


def _present_after(node: str, target_schema: Dict[str, Any]) -> bool:
    kind = _node_kind(node)
    name = _node_name(node)
    if kind == "table":
        return any(table["name"] == name for table in target_schema.get("tables", []))
    if kind == "column":
        table_name, column_name = name.split(".", 1)
        for table in target_schema.get("tables", []):
            if table["name"] == table_name:
                return any(column["name"] == column_name for column in table["columns"])
        return False
    if kind == "index":
        return any(item["name"] == name for item in target_schema.get("indexes", []))
    if kind == "view":
        return any(item["name"] == name for item in target_schema.get("views", []))
    if kind == "trigger":
        return any(item["name"] == name for item in target_schema.get("triggers", []))
    return False


def compute_invalidation(
    base_schema: Dict[str, Any],
    target_schema: Dict[str, Any],
    migration: Dict[str, Any],
    templates: Dict[str, Dict[str, Any]],
    target_version: int,
) -> Dict[str, Any]:
    graph = build_dependency_graph(base_schema, target_schema, templates)
    summaries = operation_summaries(migration)
    roots: List[Tuple[str, Dict[str, Any]]] = []
    for summary, operation in zip(summaries, migration["operations"]):
        for root in _operation_roots(operation):
            roots.append((root, summary))
    all_roots = [root for root, _summary in roots]
    reachable = graph.reachable(all_roots)

    reason_map: Dict[str, List[Dict[str, Any]]] = {}
    for root, summary in roots:
        if root not in reachable:
            paths = {root: (root, [root])}
        else:
            # One BFS from each root is needed for per-operation traceability;
            # the shared BFS above is only used for existence checks.
            paths = graph.reachable([root])
        for node, (_origin, path) in sorted(paths.items()):
            reason_map.setdefault(node, []).append(
                {
                    "operation_id": summary["op_id"],
                    "operation": summary["op"],
                    "root": root,
                    "path": path,
                }
            )

    result: Dict[str, Any] = {
        "indexes": [],
        "views": [],
        "triggers": [],
        "templates": [],
        "safe_reuse": {"indexes": [], "views": [], "triggers": [], "templates": []},
    }
    seen_nodes: Set[str] = set()
    for node in sorted(reason_map):
        kind = _node_kind(node)
        if kind not in ("index", "view", "trigger", "template"):
            continue
        name = _node_name(node)
        reasons = reason_map[node]
        unique_reasons = []
        seen_reason = set()
        for reason in reasons:
            key = (reason["operation_id"], reason["root"], tuple(reason["path"]))
            if key in seen_reason:
                continue
            seen_reason.add(key)
            unique_reasons.append(reason)
        unique_reasons.sort(
            key=lambda item: (
                item["operation_id"],
                item["root"],
                len(item["path"]),
                item["path"],
            )
        )
        if kind == "template":
            template = templates.get(name)
            if template is None:
                continue
            if int(template.get("_available_from_version", 0)) > target_version:
                continue
        item = {
            "name": name,
            "node": node,
            "present_after": _present_after(node, target_schema),
            "reasons": unique_reasons,
        }
        plural_map = {
            "index": "indexes",
            "view": "views",
            "trigger": "triggers",
            "template": "templates",
        }
        result[plural_map[kind]].append(item)
        seen_nodes.add(node)

    for kind, plural in (
        ("index", "indexes"),
        ("view", "views"),
        ("trigger", "triggers"),
    ):
        for obj in target_schema.get(plural, []):
            node = f"{kind}:{obj['name']}"
            if node not in seen_nodes:
                result["safe_reuse"][plural].append(obj["name"])
        result["safe_reuse"][plural].sort()
    for template_id, template in sorted(templates.items()):
        node = f"template:{template_id}"
        if int(template.get("_available_from_version", 0)) > target_version:
            continue
        if node not in seen_nodes:
            result["safe_reuse"]["templates"].append(template_id)
    result["safe_reuse"]["templates"].sort()
    for key in ("indexes", "views", "triggers", "templates"):
        result[key].sort(key=lambda item: item["name"])
    return result

