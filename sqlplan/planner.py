from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlglot import exp

from .errors import BusinessError, ErrorCode
from .models import PlanRecord, ViewDef
from .util import canonical_json, digest_object, sha256_text
from .validation import parse_single_select, topological_order


PLAN_FORMAT = "sqlplan/logical-plan/v1"


def _short(value: str) -> str:
    return sha256_text(value)[:12]


def _expression_sql(expression: exp.Expression) -> str:
    return expression.sql(dialect="sqlite", comments=False, pretty=False)


def _digest_expression(expression: exp.Expression) -> str:
    return "sha256:" + digest_object({"expression": _expression_sql(expression)})


def _line_column(sql: str, needle: str) -> Dict[str, Any]:
    index = sql.find(needle)
    if index < 0:
        return {"line": None, "column": None, "offset": None, "snippet": needle}
    line = sql.count("\n", 0, index) + 1
    last_newline = sql.rfind("\n", 0, index)
    column = index - last_newline
    return {"line": line, "column": column, "offset": index, "snippet": needle}


@dataclass
class Source:
    alias: str
    source_name: str
    kind: str
    outputs: List[Dict[str, Any]]
    node: Dict[str, Any]
    upstream_view: Optional[str] = None
    all_outputs: Optional[List[Dict[str, Any]]] = None

    def output_map(self) -> Dict[str, Dict[str, Any]]:
        return {column["name"]: column for column in self.outputs}


class PlanBuilder:
    def __init__(
        self,
        views: Mapping[str, ViewDef],
        *,
        upstream_plans: Optional[Mapping[str, PlanRecord]] = None,
    ) -> None:
        self.views = views
        self.upstream_plans = dict(upstream_plans or {})
        self._node_counter = 0

    def build_all(self) -> Dict[str, PlanRecord]:
        records: Dict[str, PlanRecord] = {}
        for name in topological_order(self.views):
            view = self.views[name]
            available = {upstream: records[upstream] for upstream in view.upstream}
            records[name] = self.build_view(view, upstream_plans=available)
        return records

    def build_view(
        self,
        view: ViewDef,
        *,
        upstream_plans: Optional[Mapping[str, PlanRecord]] = None,
    ) -> PlanRecord:
        self._node_counter = 0
        query = parse_single_select(view.sql, view_name=view.name)
        upstream = dict(upstream_plans or {})
        for name in view.upstream:
            if name not in upstream and name in self.upstream_plans:
                upstream[name] = self.upstream_plans[name]
        builder = _QueryBuilder(view, upstream, self.views)
        plan = builder.build(query)
        plan["view"] = view.name
        plan["format"] = PLAN_FORMAT
        plan["tree_digest"] = "sha256:" + digest_object(plan["root"])
        plan_digest = "sha256:" + digest_object(plan)
        executable = _expression_sql(query)
        return PlanRecord.from_plan(view, plan, plan_digest, executable)


class _QueryBuilder:
    def __init__(
        self,
        view: ViewDef,
        upstream_plans: Mapping[str, PlanRecord],
        views: Mapping[str, ViewDef],
    ) -> None:
        self.view = view
        self.upstream_plans = upstream_plans
        self.views = views
        self.sql = view.sql
        self.counter = 0
        self.flat_nodes: List[Dict[str, Any]] = []
        self.dependencies: Dict[str, Dict[str, Any]] = {}

    def _next_path(self) -> str:
        value = str(self.counter)
        self.counter += 1
        return value

    def _node(
        self,
        kind: str,
        *,
        outputs: Optional[List[Dict[str, Any]]] = None,
        predicates: Optional[List[Dict[str, Any]]] = None,
        join_keys: Optional[List[Dict[str, Any]]] = None,
        children: Optional[List[Dict[str, Any]]] = None,
        properties: Optional[Dict[str, Any]] = None,
        path: Optional[str] = None,
    ) -> Dict[str, Any]:
        path_value = path or self._next_path()
        node = {
            "id": f"{self.view.name}:{kind}:{path_value}",
            "kind": kind,
            "view": self.view.name,
            "children": children or [],
            "output_columns": outputs or [],
            "predicate_columns": predicates or [],
            "join_keys": join_keys or [],
            "properties": properties or {},
        }
        self.flat_nodes.append(node)
        return node

    def build(self, query: exp.Query) -> Dict[str, Any]:
        if not isinstance(query, exp.Select):
            raise BusinessError(
                ErrorCode.E_SQL_PARSE,
                "逻辑计划当前支持 SELECT 查询",
                {"view": self.view.name, "query_type": type(query).__name__},
            )

        sources, from_node = self._build_from(query)
        output = from_node["output_columns"]
        alias_expressions: Dict[str, exp.Expression] = {}
        for projection in query.expressions:
            if isinstance(projection, exp.Alias):
                alias_expressions[projection.alias] = projection.this
            elif isinstance(projection, exp.Column):
                alias_expressions[projection.name] = projection

        where = query.args.get("where")
        if where is not None:
            path = self._next_path()
            node_id = f"{self.view.name}:filter:{path}"
            refs = self._resolve_expression(
                where.this,
                sources,
                node_id=node_id,
                context="where",
            )
            output = list(output)
            from_node = self._node(
                "filter",
                outputs=output,
                predicates=refs,
                children=[from_node],
                properties={
                    "clause": "where",
                    "condition": _expression_sql(where.this),
                },
                path=path,
            )

        group = query.args.get("group")
        aggregate_expressions = [
            expression
            for projection in query.expressions
            for expression in projection.find_all(exp.AggFunc)
        ]
        if group is not None or aggregate_expressions:
            path = self._next_path()
            node_id = f"{self.view.name}:aggregate:{path}"
            group_refs: List[Dict[str, Any]] = []
            aggregate_refs: List[Dict[str, Any]] = []
            if group is not None:
                for group_expression in group.expressions:
                    group_refs.extend(
                        self._resolve_expression(
                            group_expression,
                            sources,
                            node_id=node_id,
                            context="group_by",
                            aliases=alias_expressions,
                        )
                    )
            aggregate_outputs = self._dedupe_refs_as_columns(group_refs)
            for ordinal, projection in enumerate(query.expressions, start=1):
                if not list(projection.find_all(exp.AggFunc)):
                    continue
                inner = (
                    projection.this if isinstance(projection, exp.Alias) else projection
                )
                refs = self._resolve_expression(
                    inner,
                    sources,
                    node_id=node_id,
                    context=f"aggregate[{ordinal}]",
                )
                aggregate_refs.extend(refs)
                refs = self._dedupe_refs(refs)
                name = (
                    projection.alias
                    if isinstance(projection, exp.Alias)
                    else (inner.alias_or_name or f"_aggregate_{ordinal}")
                )
                aggregate_outputs.append(
                    {
                        "name": name,
                        "ordinal": len(aggregate_outputs) + 1,
                        "expression": _expression_sql(inner),
                        "expression_digest": "sha256:"
                        + digest_object(
                            {
                                "expression": _expression_sql(inner),
                                "references": [
                                    {
                                        "upstream_view": ref.get("upstream_view"),
                                        "resolved_column": ref.get("resolved_column"),
                                        "source_expression_digest": ref.get(
                                            "source_expression_digest"
                                        ),
                                    }
                                    for ref in refs
                                ],
                            }
                        ),
                        "references": refs,
                    }
                )
            from_node = self._node(
                "aggregate",
                outputs=aggregate_outputs,
                predicates=self._dedupe_refs(group_refs + aggregate_refs),
                children=[from_node],
                properties={
                    "group_by": [
                        _expression_sql(value)
                        for value in (group.expressions if group is not None else [])
                    ],
                    "aggregate_expressions": [
                        _expression_sql(value) for value in aggregate_expressions
                    ],
                },
                path=path,
            )
            output = list(aggregate_outputs)

        having = query.args.get("having")
        if having is not None:
            path = self._next_path()
            node_id = f"{self.view.name}:filter:{path}"
            refs = self._resolve_expression(
                having.this,
                sources,
                node_id=node_id,
                context="having",
                aliases=alias_expressions,
            )
            from_node = self._node(
                "filter",
                outputs=list(from_node["output_columns"]),
                predicates=refs,
                children=[from_node],
                properties={
                    "clause": "having",
                    "condition": _expression_sql(having.this),
                },
                path=path,
            )

        project_path = self._next_path()
        project_node_id = f"{self.view.name}:project:{project_path}"
        project_refs: List[Dict[str, Any]] = []
        output_columns: List[Dict[str, Any]] = []
        for ordinal, projection in enumerate(query.expressions, start=1):
            if isinstance(projection, exp.Star) or (
                isinstance(projection, exp.Column) and projection.is_star
            ):
                qualifier = (
                    projection.table if isinstance(projection, exp.Column) else ""
                )
                star_refs = self._star_refs(
                    sources, projection, node_id=project_node_id
                )
                project_refs.extend(star_refs)
                output_columns.extend(self._star_outputs(sources, qualifier=qualifier))
                continue
            if isinstance(projection, exp.Alias):
                inner = projection.this
                name = projection.alias
            elif isinstance(projection, exp.Column):
                inner = projection
                name = projection.name
            else:
                inner = projection
                name = projection.alias_or_name or f"_expr_{ordinal}"
            refs = self._resolve_expression(
                inner,
                sources,
                node_id=project_node_id,
                context=f"select[{ordinal}]",
            )
            refs = self._dedupe_refs(refs)
            project_refs.extend(refs)
            expression_digest = "sha256:" + digest_object(
                {
                    "expression": _expression_sql(inner),
                    "references": [
                        {
                            "upstream_view": ref.get("upstream_view"),
                            "resolved_column": ref.get("resolved_column"),
                            "source_expression_digest": ref.get(
                                "source_expression_digest"
                            ),
                        }
                        for ref in refs
                    ],
                }
            )
            output_columns.append(
                {
                    "name": name,
                    "ordinal": ordinal,
                    "expression": _expression_sql(inner),
                    "expression_digest": expression_digest,
                    "references": refs,
                }
            )

        names = [column["name"] for column in output_columns]
        if len(names) != len(set(names)):
            duplicates = sorted({name for name in names if names.count(name) > 1})
            raise BusinessError(
                ErrorCode.E_BAD_VIEW,
                "视图输出列名重复",
                {"view": self.view.name, "duplicate_columns": duplicates},
            )

        project_node = self._node(
            "project",
            outputs=output_columns,
            predicates=self._dedupe_refs(project_refs),
            children=[from_node],
            properties={
                "expressions": [
                    _expression_sql(expression) for expression in query.expressions
                ]
            },
            path=project_path,
        )
        root = project_node

        order = query.args.get("order")
        if order is not None:
            path = self._next_path()
            node_id = f"{self.view.name}:sort:{path}"
            refs: List[Dict[str, Any]] = []
            for ordered in order.expressions:
                refs.extend(
                    self._resolve_expression(
                        ordered.this,
                        sources,
                        node_id=node_id,
                        context="order_by",
                        aliases=alias_expressions,
                    )
                )
            root = self._node(
                "sort",
                outputs=output_columns,
                predicates=self._dedupe_refs(refs),
                children=[root],
                properties={
                    "items": [
                        {
                            "expression": _expression_sql(ordered.this),
                            "desc": bool(ordered.args.get("desc")),
                        }
                        for ordered in order.expressions
                    ]
                },
                path=path,
            )

        limit = query.args.get("limit")
        if limit is not None:
            path = self._next_path()
            root = self._node(
                "limit",
                outputs=output_columns,
                children=[root],
                properties={"limit": _expression_sql(limit.expression)},
                path=path,
            )

        return {
            "format": PLAN_FORMAT,
            "view": self.view.name,
            "root": root,
            "nodes": self.flat_nodes,
            "output_columns": output_columns,
            "dependencies": [
                self.dependencies[name] for name in sorted(self.dependencies)
            ],
        }

    def _build_from(self, query: exp.Select) -> Tuple[List[Source], Dict[str, Any]]:
        from_expression = query.args.get("from")
        if from_expression is None:
            raise BusinessError(
                ErrorCode.E_SQL_PARSE,
                "视图查询缺少 FROM 子句",
                {"view": self.view.name},
            )
        left_source = self._source_from_expression(from_expression.this, "from")
        sources = [left_source]
        current_node = left_source.node
        for index, join in enumerate(query.args.get("joins") or []):
            path = self._next_path()
            node_id = f"{self.view.name}:join:{path}"
            right_source = self._source_from_expression(join.this, f"join_{index}")
            sources.append(right_source)
            join_keys: List[Dict[str, Any]] = []
            predicates: List[Dict[str, Any]] = []
            on = join.args.get("on")
            if on is not None:
                predicates = self._resolve_expression(
                    on,
                    sources,
                    node_id=node_id,
                    context=f"join_on[{index}]",
                )
                join_keys = self._extract_join_keys(on, predicates)
            output = list(current_node["output_columns"]) + list(
                right_source.node["output_columns"]
            )
            current_node = self._node(
                "join",
                outputs=output,
                predicates=self._dedupe_refs(predicates),
                join_keys=join_keys,
                children=[current_node, right_source.node],
                properties={
                    "join_type": str(join.args.get("kind") or "INNER").upper(),
                    "on": _expression_sql(on) if on is not None else None,
                },
                path=path,
            )
        return sources, current_node

    def _source_from_expression(self, expression: exp.Expression, path: str) -> Source:
        if isinstance(expression, exp.Table):
            source_name = expression.name
            alias = expression.alias_or_name
            if source_name in self.view.upstream:
                upstream = self.upstream_plans.get(source_name)
                if upstream is None:
                    raise BusinessError(
                        ErrorCode.E_UPSTREAM_NOT_FOUND,
                        "缺少上游逻辑计划",
                        {"view": self.view.name, "upstream": source_name},
                    )
                all_outputs = [dict(column) for column in upstream.columns]
                outputs = self._upstream_scan_outputs(
                    alias, source_name, upstream.columns
                )
                for column in all_outputs:
                    column.setdefault("qualifier", alias)
                    column.setdefault("source_view", source_name)
                for column in outputs:
                    column.setdefault("qualifier", alias)
                    column.setdefault("source_view", source_name)
                node = self._node(
                    "scan",
                    outputs=outputs,
                    path=f"scan:{path}",
                    properties={
                        "source": source_name,
                        "alias": alias,
                        "source_kind": "view",
                    },
                )
                return Source(
                    alias=alias,
                    source_name=source_name,
                    kind="view",
                    outputs=outputs,
                    node=node,
                    upstream_view=source_name,
                    all_outputs=all_outputs,
                )
            if source_name in self.views:
                raise BusinessError(
                    ErrorCode.E_BAD_VIEW,
                    "SQL 引用的视图未声明为上游",
                    {
                        "view": self.view.name,
                        "referenced_view": source_name,
                        "declared_upstream": list(self.view.upstream),
                    },
                )
            inferred = self._infer_physical_columns(alias, source_name)
            node = self._node(
                "scan",
                outputs=inferred,
                path=f"scan:{path}",
                properties={
                    "source": source_name,
                    "alias": alias,
                    "source_kind": "physical",
                },
            )
            return Source(
                alias=alias,
                source_name=source_name,
                kind="physical",
                outputs=inferred,
                node=node,
                all_outputs=inferred,
            )
        if isinstance(expression, exp.Subquery):
            inner_query = expression.this
            if not isinstance(inner_query, exp.Select):
                raise BusinessError(
                    ErrorCode.E_SQL_PARSE,
                    "FROM 子查询必须是 SELECT",
                    {"view": self.view.name},
                )
            inner = _QueryBuilder(self.view, self.upstream_plans, self.views)
            inner.counter = self.counter
            inner_plan = inner.build(inner_query)
            self.counter = inner.counter
            self.flat_nodes.extend(inner.flat_nodes)
            for name, dependency in inner.dependencies.items():
                target = self.dependencies.setdefault(
                    name,
                    {
                        "view": name,
                        "aliases": [],
                        "columns": [],
                    },
                )
                target["columns"].extend(dependency["columns"])
                target["columns"] = self._dedupe_refs(target["columns"])
                for alias in dependency.get("aliases", []):
                    if alias not in target["aliases"]:
                        target["aliases"].append(alias)
                target["aliases"].sort()
                if dependency.get("star"):
                    target["star"] = True
            alias = expression.alias_or_name
            outputs = [dict(column) for column in inner_plan["output_columns"]]
            for column in outputs:
                column.setdefault("qualifier", alias)
            node = self._node(
                "subquery",
                outputs=outputs,
                children=[inner_plan["root"]],
                properties={"alias": alias},
            )
            return Source(
                alias=alias,
                source_name=alias,
                kind="subquery",
                outputs=outputs,
                node=node,
                all_outputs=outputs,
            )
        raise BusinessError(
            ErrorCode.E_SQL_PARSE,
            "不支持的 FROM 数据源",
            {"view": self.view.name, "source_type": type(expression).__name__},
        )

    def _upstream_scan_outputs(
        self,
        alias: str,
        source_name: str,
        upstream_columns: Sequence[Mapping[str, Any]],
    ) -> List[Dict[str, Any]]:
        query = parse_single_select(self.sql, view_name=self.view.name)
        by_name = {str(column["name"]): dict(column) for column in upstream_columns}
        referenced: set[str] = set()
        expand_all = False
        for projection in query.expressions:
            if isinstance(projection, exp.Star):
                expand_all = True
            elif isinstance(projection, exp.Column) and projection.is_star:
                if not projection.table or projection.table in {
                    alias,
                    source_name,
                }:
                    expand_all = True
        for column in query.find_all(exp.Column):
            if column.is_star:
                continue
            if column.table:
                if column.table not in {alias, source_name}:
                    continue
            elif column.name not in by_name:
                continue
            referenced.add(column.name)
        names = sorted(by_name) if expand_all else sorted(referenced)
        return [by_name[name] for name in names if name in by_name]

    def _infer_physical_columns(
        self, alias: str, source_name: str
    ) -> List[Dict[str, Any]]:
        query = parse_single_select(self.sql, view_name=self.view.name)
        found: Dict[str, Dict[str, Any]] = {}
        table_count = len(list(query.find_all(exp.Table)))
        for column in query.find_all(exp.Column):
            if column.is_star:
                found["*"] = {
                    "name": "*",
                    "qualifier": alias,
                    "expression": f"{alias}.*",
                    "expression_digest": "sha256:"
                    + digest_object({"physical": source_name, "column": "*"}),
                    "source_view": None,
                    "physical_source": source_name,
                }
                continue
            table = column.table
            if table == alias or (not table and table_count == 1):
                found.setdefault(
                    column.name,
                    {
                        "name": column.name,
                        "qualifier": alias,
                        "expression": f"{alias}.{column.name}",
                        "expression_digest": "sha256:"
                        + digest_object(
                            {"physical": source_name, "column": column.name}
                        ),
                        "source_view": None,
                        "physical_source": source_name,
                    },
                )
        if not found:
            found["*"] = {
                "name": "*",
                "qualifier": alias,
                "expression": f"{alias}.*",
                "expression_digest": "sha256:"
                + digest_object({"physical": source_name, "column": "*"}),
                "source_view": None,
                "physical_source": source_name,
            }
        return [found[name] for name in sorted(found)]

    def _resolve_expression(
        self,
        expression: exp.Expression,
        sources: Sequence[Source],
        *,
        node_id: str,
        context: str,
        aliases: Optional[Mapping[str, exp.Expression]] = None,
        seen_aliases: Optional[set[str]] = None,
    ) -> List[Dict[str, Any]]:
        refs: List[Dict[str, Any]] = []
        for column in expression.find_all(exp.Column):
            if column.is_star:
                refs.extend(self._star_refs(sources, column, node_id=node_id))
            elif (
                not column.table
                and aliases is not None
                and column.name in aliases
                and column.name not in (seen_aliases or set())
            ):
                refs.extend(
                    self._resolve_expression(
                        aliases[column.name],
                        sources,
                        node_id=node_id,
                        context=context,
                        aliases=aliases,
                        seen_aliases=(seen_aliases or set()) | {column.name},
                    )
                )
            else:
                refs.append(
                    self._resolve_column(
                        column,
                        sources,
                        node_id=node_id,
                        context=context,
                    )
                )
        return self._dedupe_refs(refs)

    def _resolve_column(
        self,
        column: exp.Column,
        sources: Sequence[Source],
        *,
        node_id: str,
        context: str,
    ) -> Dict[str, Any]:
        qualifier = column.table or None
        candidates: List[Tuple[Source, Dict[str, Any]]] = []
        if qualifier:
            matching_sources = [
                source
                for source in sources
                if source.alias == qualifier or source.source_name == qualifier
            ]
            if not matching_sources:
                self._raise_column_error(
                    column,
                    sources,
                    node_id=node_id,
                    context=context,
                    message="列引用的表别名不存在",
                    extra={"reason": "unknown_qualifier"},
                )
            for source in matching_sources:
                output = source.output_map().get(column.name)
                if output is not None:
                    candidates.append((source, output))
            if not candidates:
                self._raise_column_error(
                    column,
                    sources,
                    node_id=node_id,
                    context=context,
                    message="上游视图或数据源中不存在被引用列",
                    extra={"qualifier": qualifier},
                )
        else:
            for source in sources:
                output = source.output_map().get(column.name)
                if output is not None:
                    candidates.append((source, output))
            if not candidates:
                physical = [source for source in sources if source.kind == "physical"]
                if len(physical) == 1:
                    source = physical[0]
                    output = {
                        "name": column.name,
                        "qualifier": source.alias,
                        "expression": f"{source.alias}.{column.name}",
                        "expression_digest": "sha256:"
                        + digest_object(
                            {
                                "physical": source.source_name,
                                "column": column.name,
                            }
                        ),
                    }
                    candidates.append((source, output))
                else:
                    self._raise_column_error(
                        column,
                        sources,
                        node_id=node_id,
                        context=context,
                        message="无法在数据源中解析列",
                        extra={"reason": "unresolved_unqualified"},
                    )
            if len(candidates) > 1:
                raise BusinessError(
                    ErrorCode.E_COLUMN_AMBIGUOUS,
                    "未限定列名在多个数据源中存在歧义",
                    {
                        "view": self.view.name,
                        "node_id": node_id,
                        "column": column.name,
                        "context": context,
                        "candidates": [
                            {
                                "source": source.source_name,
                                "qualifier": source.alias,
                            }
                            for source, _ in candidates
                        ],
                        "reference_position": _line_column(
                            self.sql, _expression_sql(column)
                        ),
                    },
                )

        source, output = candidates[0]
        ref = {
            "column": column.name,
            "qualifier": qualifier,
            "source_name": source.source_name,
            "source_alias": source.alias,
            "source_kind": source.kind,
            "upstream_view": source.upstream_view,
            "resolved_column": output["name"],
            "source_expression_digest": output.get(
                "expression_digest",
                "sha256:" + digest_object(output),
            ),
            "node_id": node_id,
            "context": context,
            "reference_position": _line_column(self.sql, _expression_sql(column)),
            "star": False,
        }
        if source.upstream_view:
            dependency = self.dependencies.setdefault(
                source.upstream_view,
                {
                    "view": source.upstream_view,
                    "aliases": [],
                    "columns": [],
                },
            )
            if source.alias not in dependency["aliases"]:
                dependency["aliases"].append(source.alias)
                dependency["aliases"].sort()
            dependency["columns"].append(
                {
                    "column": output["name"],
                    "source_expression_digest": ref["source_expression_digest"],
                    "node_id": node_id,
                    "context": context,
                }
            )
            dependency["columns"] = self._dedupe_refs(dependency["columns"])
        return ref

    def _star_refs(
        self,
        sources: Sequence[Source],
        expression: exp.Expression,
        *,
        node_id: str,
    ) -> List[Dict[str, Any]]:
        qualifier = expression.table if isinstance(expression, exp.Column) else ""
        selected = [
            source
            for source in sources
            if not qualifier
            or source.alias == qualifier
            or source.source_name == qualifier
        ]
        if qualifier and not selected:
            self._raise_column_error(
                expression,
                sources,
                node_id=node_id,
                context="star",
                message="星号引用的数据源不存在",
                extra={"qualifier": qualifier},
            )
        refs: List[Dict[str, Any]] = []
        for source in selected:
            outputs = source.outputs
            if source.kind == "physical" and all(
                output["name"] == "*" for output in outputs
            ):
                ref = {
                    "column": "*",
                    "qualifier": qualifier or None,
                    "source_name": source.source_name,
                    "source_alias": source.alias,
                    "source_kind": source.kind,
                    "upstream_view": source.upstream_view,
                    "resolved_column": "*",
                    "source_expression_digest": outputs[0].get(
                        "expression_digest",
                        "sha256:" + digest_object(outputs[0]),
                    ),
                    "node_id": node_id,
                    "context": "star",
                    "reference_position": _line_column(
                        self.sql, _expression_sql(expression)
                    ),
                    "star": True,
                }
                refs.append(ref)
                if source.upstream_view:
                    self._add_dependency(source, ref)
                continue
            for output in outputs:
                ref = {
                    "column": output["name"],
                    "qualifier": qualifier or None,
                    "source_name": source.source_name,
                    "source_alias": source.alias,
                    "source_kind": source.kind,
                    "upstream_view": source.upstream_view,
                    "resolved_column": output["name"],
                    "source_expression_digest": output.get(
                        "expression_digest",
                        "sha256:" + digest_object(output),
                    ),
                    "node_id": node_id,
                    "context": "star",
                    "reference_position": _line_column(
                        self.sql, _expression_sql(expression)
                    ),
                    "star": True,
                }
                refs.append(ref)
                if source.upstream_view:
                    self._add_dependency(source, ref)
        return refs

    def _add_dependency(self, source: Source, ref: Dict[str, Any]) -> None:
        if not source.upstream_view:
            return
        dependency = self.dependencies.setdefault(
            source.upstream_view,
            {"view": source.upstream_view, "aliases": [], "columns": []},
        )
        if source.alias not in dependency["aliases"]:
            dependency["aliases"].append(source.alias)
            dependency["aliases"].sort()
        column_ref = {
            "column": ref["resolved_column"],
            "source_expression_digest": ref["source_expression_digest"],
            "node_id": ref["node_id"],
            "context": ref["context"],
        }
        if ref.get("star"):
            column_ref["star"] = True
            dependency["star"] = True
        dependency["columns"].append(column_ref)
        dependency["columns"] = self._dedupe_refs(dependency["columns"])

    def _star_outputs(
        self, sources: Sequence[Source], *, qualifier: str = ""
    ) -> List[Dict[str, Any]]:
        outputs: List[Dict[str, Any]] = []
        ordinal = 1
        for source in sources:
            if (
                qualifier
                and source.alias != qualifier
                and source.source_name != qualifier
            ):
                continue
            for output in source.outputs:
                item = dict(output)
                item["ordinal"] = ordinal
                item["qualifier"] = source.alias
                if source.upstream_view:
                    item["source_view"] = source.upstream_view
                outputs.append(item)
                ordinal += 1
        return outputs

    def _extract_join_keys(
        self, condition: exp.Expression, refs: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        keys: List[Dict[str, Any]] = []
        equalities: List[exp.EQ] = []
        if isinstance(condition, exp.EQ):
            equalities.append(condition)
        equalities.extend(
            [value for value in condition.find_all(exp.EQ) if isinstance(value, exp.EQ)]
        )
        seen = set()
        for equality in equalities:
            if not isinstance(equality.this, exp.Column) or not isinstance(
                equality.expression, exp.Column
            ):
                continue
            left_sql = _expression_sql(equality.this)
            right_sql = _expression_sql(equality.expression)
            marker = (left_sql, right_sql)
            if marker in seen:
                continue
            seen.add(marker)
            left_refs = [
                ref
                for ref in refs
                if ref["column"] == equality.this.name
                and (
                    not equality.this.table
                    or ref["source_alias"] == equality.this.table
                    or ref["source_name"] == equality.this.table
                )
            ]
            right_refs = [
                ref
                for ref in refs
                if ref["column"] == equality.expression.name
                and (
                    not equality.expression.table
                    or ref["source_alias"] == equality.expression.table
                    or ref["source_name"] == equality.expression.table
                )
            ]
            if left_refs and right_refs:
                left = dict(left_refs[0])
                right = dict(right_refs[0])
                keys.append(
                    {
                        "left": {
                            "source": left.get("source_name"),
                            "alias": left.get("source_alias"),
                            "column": left.get("resolved_column"),
                        },
                        "right": {
                            "source": right.get("source_name"),
                            "alias": right.get("source_alias"),
                            "column": right.get("resolved_column"),
                        },
                    }
                )
        keys.sort(key=lambda item: canonical_json(item))
        return keys

    def _dedupe_refs_as_columns(
        self, refs: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        seen = set()
        for ordinal, ref in enumerate(refs, start=1):
            name = ref.get("resolved_column")
            if not name or name == "*" or name in seen:
                continue
            seen.add(name)
            result.append(
                {
                    "name": name,
                    "ordinal": ordinal,
                    "expression": ref.get("column"),
                    "expression_digest": ref.get("source_expression_digest"),
                    "references": [dict(ref)],
                }
            )
        return result

    def _dedupe_refs(self, refs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        seen = set()
        for ref in refs:
            key = canonical_json(
                {
                    "node_id": ref.get("node_id"),
                    "column": ref.get("column"),
                    "qualifier": ref.get("qualifier"),
                    "source_name": ref.get("source_name"),
                    "resolved_column": ref.get("resolved_column"),
                    "context": ref.get("context"),
                }
            )
            if key not in seen:
                seen.add(key)
                result.append(ref)
        return result

    def _raise_column_error(
        self,
        column: exp.Expression,
        sources: Sequence[Source],
        *,
        node_id: str,
        context: str,
        message: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        expression_sql = _expression_sql(column)
        available = {
            source.alias: sorted(
                {column["name"] for column in (source.all_outputs or source.outputs)}
            )
            for source in sources
        }
        details: Dict[str, Any] = {
            "view": self.view.name,
            "node_id": node_id,
            "column": getattr(column, "name", None) or expression_sql,
            "qualified_column": expression_sql,
            "context": context,
            "reference_position": _line_column(self.sql, expression_sql),
            "available_columns": available,
        }
        if extra:
            details.update(extra)
        raise BusinessError(ErrorCode.E_COLUMN_NOT_FOUND, message, details)


def build_plans(
    views: Mapping[str, ViewDef],
    *,
    previous: Optional[Mapping[str, PlanRecord]] = None,
) -> Dict[str, PlanRecord]:
    """Build a complete plan generation, reusing byte-identical cache entries when safe."""
    previous = previous or {}
    records: Dict[str, PlanRecord] = {}
    builder = PlanBuilder(views, upstream_plans=previous)
    for name in topological_order(views):
        view = views[name]
        old = previous.get(name)
        if (
            old is not None
            and old.sql == view.sql
            and old.upstream == list(view.upstream)
            and _old_dependencies_unchanged(old, view.upstream, records, previous)
        ):
            record = old.clone_with_definition(view)
            records[name] = record
            continue
        available = {
            upstream: records[upstream]
            for upstream in view.upstream
            if upstream in records
        }
        record = builder.build_view(view, upstream_plans=available)
        records[name] = record
    return records


def _old_dependencies_unchanged(
    old: PlanRecord,
    upstream_names: Sequence[str],
    current_records: Mapping[str, PlanRecord],
    previous_records: Mapping[str, PlanRecord],
) -> bool:
    dependencies = {item["view"]: item for item in old.plan.get("dependencies", [])}
    for upstream_name in upstream_names:
        if upstream_name not in current_records:
            return False
        dependency = dependencies.get(upstream_name)
        if dependency is None:
            continue
        current_outputs = {
            column["name"]: column for column in current_records[upstream_name].columns
        }
        for referenced in dependency.get("columns", []):
            column_name = referenced.get("column")
            current = current_outputs.get(column_name)
            if current is None:
                return False
            if current.get("expression_digest") != referenced.get(
                "source_expression_digest"
            ):
                return False
        star_dependency = bool(dependency.get("star"))
        if not star_dependency:
            star_dependency = any(
                column.get("star")
                and any(
                    ref.get("upstream_view") == upstream_name and ref.get("star")
                    for ref in column.get("references", [])
                )
                for node in old.plan.get("nodes", [])
                for column in node.get("output_columns", [])
            )
        if star_dependency:
            previous = previous_records.get(upstream_name)
            if previous is None:
                return False
            old_schema = [
                (column["name"], column.get("expression_digest"))
                for column in previous.columns
            ]
            new_schema = [
                (column["name"], column.get("expression_digest"))
                for column in current_records[upstream_name].columns
            ]
            if old_schema != new_schema:
                return False
    return True
