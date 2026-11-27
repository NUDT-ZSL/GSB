"""Logical plan AST, normalization, and common-subexpression extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Iterator, TypeAlias

from .config import EVENT_TABLE, TABLE_COLUMNS, VEHICLE_TABLE, WATER_TABLE
from .util import canonical_json, digest_json, short_digest


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "op": self.op, "value": self.value}

    def sort_key(self) -> str:
        return canonical_json(self.to_dict())


@dataclass(frozen=True)
class Metric:
    func: str
    field: str
    alias: str

    def to_dict(self) -> dict[str, str]:
        return {"func": self.func, "field": self.field, "as": self.alias}


@dataclass(frozen=True)
class OutputColumn:
    field: str
    alias: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "as": self.alias}


@dataclass(frozen=True)
class Scan:
    alias: str
    table: str


@dataclass(frozen=True)
class Filter:
    child: "PlanNode"
    conditions: tuple[Condition, ...]


@dataclass(frozen=True)
class Join:
    left: "PlanNode"
    right: "PlanNode"
    left_key: str
    right_key: str


@dataclass(frozen=True)
class Aggregate:
    child: "PlanNode"
    group_by: tuple[str, ...]
    metrics: tuple[Metric, ...]


@dataclass(frozen=True)
class Project:
    child: "PlanNode"
    columns: tuple[OutputColumn, ...]


PlanNode: TypeAlias = Scan | Filter | Join | Aggregate | Project


@dataclass(frozen=True)
class CseCandidate:
    fingerprint: str
    candidate_id: str
    node: PlanNode
    plan_json: str
    refs: tuple[dict[str, str], ...]
    reference_count: int


def _condition_from_raw(raw: dict[str, Any]) -> Condition:
    op = str(raw["op"])
    value = raw.get("value")
    if op == "in":
        if not isinstance(value, (list, tuple)):
            raise ValueError("'in' condition value must be a list")
        value = tuple(sorted(value, key=lambda item: canonical_json(item)))
    return Condition(str(raw["field"]), op, value)


def _canonical_node(node: PlanNode) -> PlanNode:
    if isinstance(node, Scan):
        if node.table not in TABLE_COLUMNS:
            raise ValueError(f"unknown table in scan: {node.table}")
        return node
    if isinstance(node, Filter):
        child = _canonical_node(node.child)
        conditions = list(node.conditions)
        if isinstance(child, Filter):
            conditions.extend(child.conditions)
            child = child.child
        normalized: list[Condition] = []
        for condition in conditions:
            if condition.op == "in" and isinstance(condition.value, (list, tuple)):
                condition = Condition(
                    condition.field,
                    "in",
                    tuple(sorted(condition.value, key=lambda item: canonical_json(item))),
                )
            normalized.append(condition)
        normalized.sort(key=lambda item: item.sort_key())
        if not normalized:
            return child
        return Filter(child, tuple(normalized))
    if isinstance(node, Join):
        left = _canonical_node(node.left)
        right = _canonical_node(node.right)
        left_key = node.left_key
        right_key = node.right_key
        if canonical_json(_node_to_dict(left)) > canonical_json(_node_to_dict(right)):
            left, right = right, left
            left_key, right_key = right_key, left_key
        return Join(left, right, left_key, right_key)
    if isinstance(node, Aggregate):
        child = _canonical_node(node.child)
        group_by = tuple(sorted(node.group_by))
        metrics = tuple(sorted(node.metrics, key=lambda item: item.alias))
        return Aggregate(child, group_by, metrics)
    if isinstance(node, Project):
        return Project(_canonical_node(node.child), node.columns)
    raise TypeError(f"unknown plan node: {type(node)!r}")


def canonicalize_plan(node: PlanNode) -> PlanNode:
    """Put commutative operators in one stable order."""

    return _canonical_node(node)


def _node_to_dict(node: PlanNode) -> dict[str, Any]:
    if isinstance(node, Scan):
        return {"type": "scan", "alias": node.alias, "table": node.table}
    if isinstance(node, Filter):
        return {
            "type": "filter",
            "conditions": [condition.to_dict() for condition in node.conditions],
            "child": _node_to_dict(node.child),
        }
    if isinstance(node, Join):
        return {
            "type": "join",
            "left_key": node.left_key,
            "right_key": node.right_key,
            "left": _node_to_dict(node.left),
            "right": _node_to_dict(node.right),
        }
    if isinstance(node, Aggregate):
        return {
            "type": "aggregate",
            "group_by": list(node.group_by),
            "metrics": [metric.to_dict() for metric in node.metrics],
            "child": _node_to_dict(node.child),
        }
    if isinstance(node, Project):
        return {
            "type": "project",
            "columns": [column.to_dict() for column in node.columns],
            "child": _node_to_dict(node.child),
        }
    raise TypeError(f"unknown plan node: {type(node)!r}")


def plan_to_dict(node: PlanNode) -> dict[str, Any]:
    return _node_to_dict(node)


def plan_to_json(node: PlanNode) -> str:
    return canonical_json(_node_to_dict(node))


def plan_fingerprint(node: PlanNode) -> str:
    return digest_json(_node_to_dict(node))


def _event_conditions(spec: dict[str, Any]) -> list[Condition]:
    conditions: list[Condition] = []
    if "from_ms" in spec:
        conditions.append(Condition("e.occurred_at_ms", "ge", int(spec["from_ms"])))
    if "to_ms" in spec:
        conditions.append(Condition("e.occurred_at_ms", "lt", int(spec["to_ms"])))
    if spec.get("event_types"):
        conditions.append(
            Condition(
                "e.event_type",
                "in",
                tuple(sorted(str(item) for item in spec["event_types"])),
            )
        )
    if "min_severity" in spec:
        conditions.append(Condition("e.severity", "ge", int(spec["min_severity"])))
    if "max_severity" in spec:
        conditions.append(Condition("e.severity", "le", int(spec["max_severity"])))
    if spec.get("station_ids"):
        conditions.append(
            Condition(
                "e.station_id",
                "in",
                tuple(sorted(str(item) for item in spec["station_ids"])),
            )
        )
    if spec.get("district"):
        conditions.append(Condition("e.district", "eq", str(spec["district"])))
    if spec.get("incident_ids"):
        conditions.append(
            Condition(
                "e.incident_id",
                "in",
                tuple(sorted(str(item) for item in spec["incident_ids"])),
            )
        )
    if spec.get("source_versions"):
        conditions.append(
            Condition(
                "e.source_version",
                "in",
                tuple(sorted(int(item) for item in spec["source_versions"])),
            )
        )
    for raw in spec.get("conditions", []):
        conditions.append(_condition_from_raw(raw))
    return conditions


def _join_conditions(table: str, alias: str, spec: dict[str, Any]) -> list[Condition]:
    filters = spec.get("filter", {})
    conditions: list[Condition] = []
    if table == VEHICLE_TABLE:
        if filters.get("status"):
            conditions.append(
                Condition(
                    f"{alias}.status",
                    "in",
                    tuple(sorted(str(item) for item in filters["status"])),
                )
            )
        if "min_capacity" in filters:
            conditions.append(
                Condition(f"{alias}.capacity", "ge", int(filters["min_capacity"]))
            )
    elif table == WATER_TABLE:
        if "min_capacity_tons" in filters:
            conditions.append(
                Condition(
                    f"{alias}.capacity_tons",
                    "ge",
                    int(filters["min_capacity_tons"]),
                )
            )
        if "min_pressure" in filters:
            conditions.append(
                Condition(f"{alias}.pressure", "ge", float(filters["min_pressure"]))
            )
    for raw in spec.get("conditions", []):
        conditions.append(_condition_from_raw(raw))
    return conditions


def _default_project(node: PlanNode) -> tuple[OutputColumn, ...]:
    if isinstance(node, Aggregate):
        columns = [OutputColumn(field, field.split(".")[-1]) for field in node.group_by]
        columns.extend(OutputColumn(metric.alias, metric.alias) for metric in node.metrics)
        return tuple(columns)
    alias = _root_alias(node)
    return tuple(
        OutputColumn(f"{alias}.{column}", column)
        for column in TABLE_COLUMNS[EVENT_TABLE]
    )


def _root_alias(node: PlanNode) -> str:
    if isinstance(node, Scan):
        return node.alias
    if isinstance(node, (Filter, Aggregate, Project)):
        return _root_alias(node.child)
    return _root_alias(node.left)


def build_plan(spec: dict[str, Any]) -> PlanNode:
    """Parse the small query DSL into a normalized logical plan."""

    node: PlanNode = Scan("e", EVENT_TABLE)
    event_filter = spec.get("event_filter", {})
    event_conditions = _event_conditions(event_filter)
    if event_conditions:
        node = Filter(node, tuple(event_conditions))

    joins = list(spec.get("joins", []))
    join_tables = [str(item["table"]) for item in joins]
    if len(set(join_tables)) != len(join_tables):
        raise ValueError("duplicate join table")
    for join_spec in sorted(joins, key=lambda item: str(item["table"])):
        table = str(join_spec["table"])
        if table == VEHICLE_TABLE:
            alias = "v"
            right: PlanNode = Scan(alias, VEHICLE_TABLE)
            left_key, right_key = "e.vehicle_id", "v.vehicle_id"
        elif table == WATER_TABLE:
            alias = "w"
            right = Scan(alias, WATER_TABLE)
            left_key, right_key = "e.district", "w.district"
        else:
            raise ValueError(f"unsupported join table: {table}")
        node = Join(node, right, left_key, right_key)
        conditions = _join_conditions(table, alias, join_spec)
        if conditions:
            node = Filter(node, tuple(conditions))

    aggregate_spec = spec.get("aggregate")
    if aggregate_spec:
        group_by = tuple(sorted(str(item) for item in aggregate_spec.get("group_by", [])))
        metrics = tuple(
            Metric(
                func=str(item["func"]).lower(),
                field=str(item.get("field", "*")),
                alias=str(item["as"]),
            )
            for item in aggregate_spec.get("metrics", [])
        )
        if not metrics:
            raise ValueError("aggregate requires at least one metric")
        node = Aggregate(node, group_by, metrics)

    project_spec = spec.get("project")
    if project_spec:
        columns = tuple(
            OutputColumn(str(item["field"]), str(item["as"]))
            for item in project_spec
        )
    else:
        columns = _default_project(node)
    node = Project(node, columns)
    return canonicalize_plan(node)


parse_query = build_plan


def iter_subtrees(
    node: PlanNode,
    path: str = "$",
) -> Iterator[tuple[str, PlanNode, str]]:
    yield plan_fingerprint(node), node, path
    if isinstance(node, Filter):
        yield from iter_subtrees(node.child, f"{path}.filter.child")
    elif isinstance(node, Join):
        yield from iter_subtrees(node.left, f"{path}.join.left")
        yield from iter_subtrees(node.right, f"{path}.join.right")
    elif isinstance(node, Aggregate):
        yield from iter_subtrees(node.child, f"{path}.aggregate.child")
    elif isinstance(node, Project):
        yield from iter_subtrees(node.child, f"{path}.project.child")


def extract_cse_candidates(
    query_plans: dict[str, PlanNode],
) -> list[CseCandidate]:
    """Return repeated subtrees referenced by at least two different queries."""

    references: dict[str, dict[str, dict[str, str]]] = {}
    nodes: dict[str, PlanNode] = {}
    for query_id, plan in query_plans.items():
        for fingerprint, node, path in iter_subtrees(plan):
            nodes[fingerprint] = node
            references.setdefault(fingerprint, {})[f"{query_id}:{path}"] = {
                "query_id": query_id,
                "node_path": path,
            }

    candidates: list[CseCandidate] = []
    for fingerprint, ref_map in references.items():
        refs = tuple(sorted(ref_map.values(), key=lambda item: (item["query_id"], item["node_path"])))
        distinct_queries = {item["query_id"] for item in refs}
        if len(distinct_queries) < 2:
            continue
        node = nodes[fingerprint]
        candidates.append(
            CseCandidate(
                fingerprint=fingerprint,
                candidate_id=f"cse_{short_digest(fingerprint)}",
                node=node,
                plan_json=plan_to_json(node),
                refs=refs,
                reference_count=len(refs),
            )
        )
    return sorted(candidates, key=lambda item: item.fingerprint)


def collect_scans(node: PlanNode) -> dict[str, str]:
    result: dict[str, str] = {}
    if isinstance(node, Scan):
        result[node.alias] = node.table
    elif isinstance(node, Filter):
        result.update(collect_scans(node.child))
    elif isinstance(node, Join):
        result.update(collect_scans(node.left))
        result.update(collect_scans(node.right))
    elif isinstance(node, (Aggregate, Project)):
        result.update(collect_scans(node.child))
    return result


def collect_conditions(node: PlanNode) -> list[Condition]:
    result: list[Condition] = []
    if isinstance(node, Filter):
        result.extend(node.conditions)
        result.extend(collect_conditions(node.child))
    elif isinstance(node, Join):
        result.extend(collect_conditions(node.left))
        result.extend(collect_conditions(node.right))
    elif isinstance(node, (Aggregate, Project)):
        result.extend(collect_conditions(node.child))
    return result


def event_source_versions(node: PlanNode) -> tuple[int, ...]:
    """Infer the v1/v2/v3 blocks read by an incident scan.

    Conflicting constraints intentionally produce an empty tuple.
    """

    scans = collect_scans(node)
    incident_aliases = {
        alias for alias, table in scans.items() if table == EVENT_TABLE
    }
    if not incident_aliases:
        return ()
    constraints: set[int] | None = None
    for condition in collect_conditions(node):
        prefix, _, _field = condition.field.partition(".")
        if prefix not in incident_aliases or condition.field != f"{prefix}.source_version":
            continue
        if condition.op == "eq":
            values = {int(condition.value)}
        elif condition.op == "in":
            values = {int(item) for item in condition.value}
        else:
            continue
        constraints = values if constraints is None else constraints & values
    if constraints is None:
        return (1, 2, 3)
    return tuple(sorted(constraints))


def used_tables(node: PlanNode) -> frozenset[str]:
    return frozenset(collect_scans(node).values())


def query_ids_from_refs(refs: Iterable[dict[str, str]]) -> tuple[str, ...]:
    return tuple(sorted({ref["query_id"] for ref in refs}))
