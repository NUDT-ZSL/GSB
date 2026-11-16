"""Logical-plan construction and stable serialization."""

from __future__ import annotations

from typing import Any, Callable

from .errors import E_PARSE, LabError
from .parser import (
    ColumnRef,
    Condition,
    Query,
    TableBinding,
    condition_columns,
    condition_to_sql,
)
from .util import canonical_json, json_digest, sql_quote_identifier


SchemaProvider = Callable[[str], dict[str, Any] | None]


def _fail(message: str, **details: Any) -> None:
    raise LabError(E_PARSE, message, details)


def _require_table(binding: TableBinding, schemas: dict[str, dict[str, Any]]) -> None:
    if binding.table not in schemas:
        _fail("table does not exist", table=binding.table)


def _resolve_column(
    column: ColumnRef,
    schemas: dict[str, dict[str, Any]],
    alias_to_table: dict[str, str],
) -> tuple[str, str]:
    if column.qualifier is not None:
        if column.qualifier not in alias_to_table:
            _fail("unknown table alias", alias=column.qualifier, column=column.name)
        table = alias_to_table[column.qualifier]
        return table, column.name
    matches = []
    for table in alias_to_table.values():
        names = {item["name"] for item in schemas[table]["columns"]}
        if column.name in names:
            matches.append(table)
    unique = sorted(set(matches))
    if not unique:
        _fail("unknown column", column=column.name)
    if len(unique) > 1:
        _fail("ambiguous unqualified column", column=column.name, tables=unique)
    return unique[0], column.name


def _validate_condition(
    condition: Condition | None,
    schemas: dict[str, dict[str, Any]],
    alias_to_table: dict[str, str],
) -> None:
    from .parser import BoolExpr, Comparison, InList

    if condition is None:
        return
    if isinstance(condition, Comparison):
        table, name = _resolve_column(condition.left, schemas, alias_to_table)
        names = {item["name"] for item in schemas[table]["columns"]}
        if name not in names:
            _fail("unknown column", table=table, column=name)
        if isinstance(condition.right, ColumnRef):
            right_table, right_name = _resolve_column(condition.right, schemas, alias_to_table)
            right_names = {item["name"] for item in schemas[right_table]["columns"]}
            if right_name not in right_names:
                _fail("unknown column", table=right_table, column=right_name)
        return
    if isinstance(condition, InList):
        table, name = _resolve_column(condition.column, schemas, alias_to_table)
        names = {item["name"] for item in schemas[table]["columns"]}
        if name not in names:
            _fail("unknown column", table=table, column=name)
        return
    if isinstance(condition, BoolExpr):
        if not condition.children:
            _fail("empty boolean expression")
        for child in condition.children:
            _validate_condition(child, schemas, alias_to_table)
        return
    _fail("unknown predicate")


def validate_query(query: Query, schemas: dict[str, dict[str, Any]]) -> None:
    tables = query.tables()
    for binding in tables:
        _require_table(binding, schemas)
    aliases = [binding.alias for binding in tables]
    if len(set(aliases)) != len(aliases):
        _fail("duplicate table alias")
    alias_to_table = {binding.alias: binding.table for binding in tables}
    for item in query.select_items:
        if isinstance(item, str):
            continue
        table, name = _resolve_column(item, schemas, alias_to_table)
        if name not in {column["name"] for column in schemas[table]["columns"]}:
            _fail("unknown selected column", table=table, column=name)
    _validate_condition(query.join_condition, schemas, alias_to_table)
    _validate_condition(query.where, schemas, alias_to_table)
    if query.join_table is not None and query.join_condition is not None:
        joined_aliases: set[str] = set()
        for column in condition_columns(query.join_condition):
            if column.qualifier is not None:
                joined_aliases.add(column.qualifier)
                continue
            table = _resolve_column(column, schemas, alias_to_table)[0]
            matches = [binding.alias for binding in tables if binding.table == table]
            if len(matches) == 1:
                joined_aliases.add(matches[0])
        if not {query.from_table.alias, query.join_table.alias}.issubset(joined_aliases):
            _fail("join condition must reference both tables")


def build_plan(query: Query, schemas: dict[str, dict[str, Any]]) -> dict[str, Any]:
    validate_query(query, schemas)
    bindings = query.tables()
    nodes: list[dict[str, Any]] = []

    def add_node(kind: str, children: list[str], **attrs: Any) -> str:
        op_id = f"op_{len(nodes):03d}"
        node = {"op_id": op_id, "kind": kind, "children": children, **attrs}
        nodes.append(node)
        return op_id

    scan_ids: dict[str, str] = {}
    for binding in bindings:
        scan_ids[binding.alias] = add_node(
            "scan",
            [],
            table=binding.table,
            alias=binding.alias,
            label=f"SCAN {binding.table} AS {binding.alias}",
        )

    if query.join_table is None:
        root = scan_ids[query.from_table.alias]
    else:
        left = scan_ids[query.from_table.alias]
        right = scan_ids[query.join_table.alias]
        join_predicate = query.join_condition
        root = add_node(
            "join",
            [left, right],
            join_type="inner",
            predicate=join_predicate.to_dict() if join_predicate else None,
            predicate_text=join_predicate.canonical() if join_predicate else "TRUE",
            sql_predicate=condition_to_sql(join_predicate),
            label="JOIN",
        )
    if query.where is not None:
        root = add_node(
            "filter",
            [root],
            predicate=query.where.to_dict(),
            predicate_text=query.where.canonical(),
            sql_predicate=condition_to_sql(query.where),
            label="FILTER",
        )

    tables = [binding.to_dict() for binding in bindings]
    core = {
        "format": 1,
        "normalized_sql": query.normalized_sql(),
        "semantic_signature": query.semantic_signature(),
        "tables": tables,
        "root": root,
        "nodes": nodes,
    }
    plan_id = "lp_" + json_digest(core)[:24]
    plan = {**core, "plan_id": plan_id}
    plan["topological_order"] = [node["op_id"] for node in nodes]
    plan["tree"] = tree_lines(plan)
    return plan


def node_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {node["op_id"]: node for node in plan["nodes"]}


def tree_lines(plan: dict[str, Any]) -> list[str]:
    nodes = node_map(plan)

    def render(op_id: str, depth: int) -> list[str]:
        node = nodes[op_id]
        if node["kind"] == "scan":
            suffix = f" table={node['table']} alias={node['alias']}"
        else:
            suffix = f" predicate={node.get('predicate_text', '')}"
        lines = ["  " * depth + f"{node['op_id']} {node['kind'].upper()}{suffix}"]
        for child in node["children"]:
            lines.extend(render(child, depth + 1))
        return lines

    return render(plan["root"], 0)


def query_count_sql(query: Query, include_join: bool = True, include_where: bool = True) -> str:
    from_table = query.from_table
    pieces = [
        "SELECT COUNT(*) AS row_count FROM",
        sql_quote_identifier(from_table.table),
        "AS",
        sql_quote_identifier(from_table.alias),
    ]
    if include_join and query.join_table is not None:
        pieces.extend(
            [
                "JOIN",
                sql_quote_identifier(query.join_table.table),
                "AS",
                sql_quote_identifier(query.join_table.alias),
                "ON",
                condition_to_sql(query.join_condition),
            ]
        )
    if include_where and query.where is not None:
        pieces.extend(["WHERE", condition_to_sql(query.where)])
    return " ".join(pieces)


def scan_count_sql(table: str) -> str:
    return f"SELECT COUNT(*) AS row_count FROM {sql_quote_identifier(table)}"


def format_plan_tree(plan: dict[str, Any]) -> str:
    return "\n".join(plan["tree"])


def plan_digest(plan: dict[str, Any]) -> str:
    return json_digest({key: value for key, value in plan.items() if key != "tree"})
