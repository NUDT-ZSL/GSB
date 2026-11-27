"""In-memory relational execution of normalized logical plans."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from .config import EVENT_TABLE, TABLE_COLUMNS
from .plan import (
    Aggregate,
    Condition,
    Filter,
    Join,
    Metric,
    PlanNode,
    Project,
    Scan,
    plan_fingerprint,
)
from .storage import Store
from .util import canonical_bytes, canonical_json

IncidentRef = tuple[str, int]
Lineage = frozenset[IncidentRef]


@dataclass(frozen=True)
class Relation:
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    row_lineage: tuple[Lineage, ...]

    def scope(self) -> set[IncidentRef]:
        result: set[IncidentRef] = set()
        for lineage in self.row_lineage:
            result.update(lineage)
        return result


def relation_blob(relation: Relation) -> bytes:
    return canonical_bytes(
        {
            "columns": list(relation.columns),
            "rows": list(relation.rows),
        }
    )


def relation_digest(relation: Relation) -> str:
    return hashlib.sha256(relation_blob(relation)).hexdigest()


def relation_from_parts(
    columns: Iterable[str],
    rows: Iterable[dict[str, Any]],
    row_lineage: Iterable[Iterable[IncidentRef]] | None = None,
) -> Relation:
    column_tuple = tuple(columns)
    row_list = list(rows)
    lineage_list = (
        [frozenset(item) for item in row_lineage]
        if row_lineage is not None
        else [frozenset() for _ in row_list]
    )
    if len(row_list) != len(lineage_list):
        raise ValueError("row and lineage counts differ")
    pairs = list(zip(row_list, lineage_list))
    pairs.sort(
        key=lambda pair: tuple(
            canonical_json(pair[0].get(column)) for column in column_tuple
        )
    )
    return Relation(
        columns=column_tuple,
        rows=tuple(pair[0] for pair in pairs),
        row_lineage=tuple(pair[1] for pair in pairs),
    )


def relation_from_stored(
    blob: bytes,
    lineage_json: str,
) -> Relation:
    payload = json.loads(blob.decode("utf-8"))
    lineage = [
        frozenset((str(item[0]), int(item[1])) for item in group)
        for group in json.loads(lineage_json)
    ]
    return Relation(
        columns=tuple(payload["columns"]),
        rows=tuple(payload["rows"]),
        row_lineage=tuple(lineage),
    )


def lineage_json(relation: Relation) -> str:
    return canonical_json(
        [
            sorted([incident_id, occurred_at_ms] for incident_id, occurred_at_ms in group)
            for group in relation.row_lineage
        ]
    )


def relation_scope_json(relation: Relation) -> list[list[Any]]:
    return sorted([incident_id, occurred_at_ms] for incident_id, occurred_at_ms in relation.scope())


def _matches(condition: Condition, row: dict[str, Any]) -> bool:
    if condition.field not in row:
        raise KeyError(f"field not present in relation: {condition.field}")
    value = row[condition.field]
    target = condition.value
    op = condition.op
    if op == "eq":
        return value == target
    if op == "ne":
        return value != target
    if op == "in":
        return value in target
    if op == "lt":
        return value < target
    if op == "le":
        return value <= target
    if op == "gt":
        return value > target
    if op == "ge":
        return value >= target
    if op == "contains":
        return str(target) in str(value)
    raise ValueError(f"unsupported filter operator: {op}")


def _metric_value(metric: Metric, rows: list[dict[str, Any]]) -> Any:
    func = metric.func
    if func == "count":
        if metric.field == "*":
            return len(rows)
        return sum(1 for row in rows if row.get(metric.field) is not None)
    values = [row.get(metric.field) for row in rows if row.get(metric.field) is not None]
    if func == "sum":
        return sum(values) if values else 0
    if func == "avg":
        return round(sum(values) / len(values), 6) if values else None
    if func == "min":
        return min(values) if values else None
    if func == "max":
        return max(values) if values else None
    raise ValueError(f"unsupported aggregate function: {func}")


class Executor:
    def __init__(
        self,
        store: Store,
        materialized: dict[str, Relation] | None = None,
        use_cache: bool = True,
    ) -> None:
        self.store = store
        self.materialized = materialized or {}
        self.use_cache = use_cache
        self.cache_hits: Counter[str] = Counter()

    def evaluate(self, node: PlanNode, skip_cache_fp: str | None = None) -> Relation:
        fingerprint = plan_fingerprint(node)
        if (
            self.use_cache
            and skip_cache_fp != fingerprint
            and fingerprint in self.materialized
        ):
            self.cache_hits[fingerprint] += 1
            return self.materialized[fingerprint]

        if isinstance(node, Scan):
            return self._scan(node)
        if isinstance(node, Filter):
            return self._filter(node)
        if isinstance(node, Join):
            return self._join(node)
        if isinstance(node, Aggregate):
            return self._aggregate(node)
        if isinstance(node, Project):
            return self._project(node)
        raise TypeError(f"unknown plan node: {type(node)!r}")

    def _scan(self, node: Scan) -> Relation:
        if node.table not in TABLE_COLUMNS:
            raise ValueError(f"unknown table: {node.table}")
        source_rows = self.store.fetch_table(node.table)
        columns = tuple(f"{node.alias}.{column}" for column in TABLE_COLUMNS[node.table])
        rows: list[dict[str, Any]] = []
        lineage: list[Lineage] = []
        for source in source_rows:
            row = {
                f"{node.alias}.{column}": source[column]
                for column in TABLE_COLUMNS[node.table]
            }
            rows.append(row)
            if node.table == EVENT_TABLE:
                lineage.append(
                    frozenset(
                        {
                            (
                                str(source["incident_id"]),
                                int(source["occurred_at_ms"]),
                            )
                        }
                    )
                )
            else:
                lineage.append(frozenset())
        return relation_from_parts(columns, rows, lineage)

    def _filter(self, node: Filter) -> Relation:
        child = self.evaluate(node.child)
        rows: list[dict[str, Any]] = []
        lineage: list[Lineage] = []
        for row, row_lineage in zip(child.rows, child.row_lineage):
            if all(_matches(condition, row) for condition in node.conditions):
                rows.append(row)
                lineage.append(row_lineage)
        return relation_from_parts(child.columns, rows, lineage)

    def _join(self, node: Join) -> Relation:
        left = self.evaluate(node.left)
        right = self.evaluate(node.right)
        index: dict[Any, list[int]] = defaultdict(list)
        for right_index, row in enumerate(right.rows):
            index[row[node.right_key]].append(right_index)
        columns = left.columns + right.columns
        rows: list[dict[str, Any]] = []
        lineage: list[Lineage] = []
        for left_index, left_row in enumerate(left.rows):
            key = left_row[node.left_key]
            for right_index in index.get(key, ()):
                merged = dict(left_row)
                merged.update(right.rows[right_index])
                rows.append(merged)
                lineage.append(
                    left.row_lineage[left_index] | right.row_lineage[right_index]
                )
        return relation_from_parts(columns, rows, lineage)

    def _aggregate(self, node: Aggregate) -> Relation:
        child = self.evaluate(node.child)
        groups: dict[tuple[Any, ...], list[int]] = {}
        for index, row in enumerate(child.rows):
            key = tuple(row[field] for field in node.group_by)
            groups.setdefault(key, []).append(index)

        if not node.group_by and not groups:
            groups[()] = []

        columns = node.group_by + tuple(metric.alias for metric in node.metrics)
        rows: list[dict[str, Any]] = []
        lineage: list[Lineage] = []
        for key, indices in sorted(groups.items(), key=lambda item: canonical_json(item[0])):
            group_rows = [child.rows[index] for index in indices]
            row: dict[str, Any] = {
                field: value for field, value in zip(node.group_by, key)
            }
            for metric in node.metrics:
                row[metric.alias] = _metric_value(metric, group_rows)
            group_lineage: set[IncidentRef] = set()
            for index in indices:
                group_lineage.update(child.row_lineage[index])
            rows.append(row)
            lineage.append(frozenset(group_lineage))
        return relation_from_parts(columns, rows, lineage)

    def _project(self, node: Project) -> Relation:
        child = self.evaluate(node.child)
        columns = tuple(column.alias for column in node.columns)
        rows = [
            {column.alias: row[column.field] for column in node.columns}
            for row in child.rows
        ]
        return relation_from_parts(columns, rows, child.row_lineage)


def evaluate_reference(store: Store, plan: PlanNode) -> Relation:
    """Full single-query recomputation with no materialized CSE access."""

    return Executor(store, materialized=None, use_cache=False).evaluate(plan)
