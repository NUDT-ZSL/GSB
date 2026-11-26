"""View definitions, snapshot validation, and cross-version plan merging."""

from __future__ import annotations

import heapq
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    BusinessError,
    E_DEPENDENCY_CYCLE,
    E_DEPENDENCY_DECLARED_MISMATCH,
    E_DEPENDENCY_MISSING,
    E_DEPENDENCY_SELF,
    E_INPUT_SCHEMA,
    E_LOGIC_CLOCK_INVALID,
    E_PLAN_GENERATION_REGRESSION,
    E_SHARD_HASH_MISMATCH,
    E_SQL_SYNTAX,
    E_V1_COLUMN_UNKNOWN,
    E_V1_TIME_INVALID,
    E_V2_META_MISMATCH,
    E_VIEW_MISSING,
    E_VIEW_NAME_DUPLICATE,
    E_VIEW_NAME_EMPTY,
    E_VIEW_NAME_INVALID,
)
from .sqlplan import extract_sql_sources, parse_sql_plan
from .utils import (
    canonical_json,
    dedupe_preserving_order,
    normalize_sql,
    read_json_file,
    require_dict,
    require_list,
    require_nonnegative_int,
    sha256_bytes,
    sha256_json,
    valid_identifier,
)


@dataclass
class ViewDefinition:
    name: str
    sql: str
    declared_upstreams: list[str]
    dependencies: list[str]
    plan: dict[str, Any]
    columns: list[str]
    column_dependencies: dict[str, list[dict[str, str]]]
    executable_sql: str


@dataclass
class DefinitionSet:
    sources: dict[str, list[str]]
    views: dict[str, ViewDefinition]
    order: list[str]
    raw: dict[str, Any]
    source_path: str | None = None

    @property
    def view_names(self) -> set[str]:
        return set(self.views)

    def source_schemas(self, include_views: bool = True) -> dict[str, list[str]]:
        schemas = {name: list(columns) for name, columns in self.sources.items()}
        if include_views:
            for name in self.order:
                schemas[name] = list(self.views[name].columns)
        return schemas


@dataclass
class SnapshotView:
    name: str
    sql: str
    source_version: str
    clock_ms: int
    plan_generation: int
    original_time: dict[str, Any]
    conversion: dict[str, Any] | None
    plan: dict[str, Any]
    declared_dependencies: list[str] = field(default_factory=list)
    declared_columns: list[str] = field(default_factory=list)
    declared_column_dependencies: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    shard_id: str | None = None
    sample_order: int = 0


@dataclass
class Snapshot:
    format_version: int
    logical_clock_ms: int
    snapshot_generation: int
    views: list[SnapshotView] = field(default_factory=list)
    tombstones: list[SnapshotView] = field(default_factory=list)
    source_path: str | None = None
    source_hashes: dict[str, str] = field(default_factory=dict)


def _normalize_columns(value: Any, *, field_name: str) -> list[str]:
    columns = require_list(value, field_name)
    normalized: list[str] = []
    seen: set[str] = set()
    for column in columns:
        if not isinstance(column, str) or not column:
            raise BusinessError(E_INPUT_SCHEMA, f"{field_name} 中包含空列名", field=field_name)
        if column in seen:
            raise BusinessError(
                E_INPUT_SCHEMA,
                f"{field_name} 中列名重复: {column}",
                field=field_name,
                column=column,
            )
        seen.add(column)
        normalized.append(column)
    return normalized


def _normalize_sources(value: Any) -> dict[str, list[str]]:
    source_obj = require_dict(value, "sources")
    sources: dict[str, list[str]] = {}
    for name, columns_value in source_obj.items():
        if not valid_identifier(name):
            raise BusinessError(E_INPUT_SCHEMA, f"源表名不合法: {name}", source=name)
        if isinstance(columns_value, dict):
            columns_value = columns_value.get("columns")
        sources[name] = _normalize_columns(
            columns_value,
            field_name=f"sources.{name}",
        )
    return sources


def _declared_upstreams(item: dict[str, Any], *, view: str) -> list[str]:
    value = item.get("upstreams", item.get("dependencies", []))
    if value is None:
        return []
    upstreams = require_list(value, f"views[{view}].upstreams", view=view)
    result: list[str] = []
    for upstream in upstreams:
        if not isinstance(upstream, str) or not upstream:
            raise BusinessError(
                E_INPUT_SCHEMA,
                "上游视图名必须是非空字符串",
                view=view,
                field="upstreams",
            )
        if upstream not in result:
            result.append(upstream)
    return result


def _find_cycle(edges: dict[str, list[str]]) -> list[str]:
    state: dict[str, int] = {name: 0 for name in edges}
    stack: list[str] = []
    positions: dict[str, int] = {}

    def visit(name: str) -> list[str] | None:
        state[name] = 1
        positions[name] = len(stack)
        stack.append(name)
        for dependency in sorted(edges.get(name, [])):
            if state.get(dependency, 0) == 0:
                found = visit(dependency)
                if found:
                    return found
            elif state.get(dependency) == 1:
                start = positions.get(dependency, 0)
                return stack[start:] + [dependency]
        stack.pop()
        positions.pop(name, None)
        state[name] = 2
        return None

    for name in sorted(edges):
        if state.get(name, 0) == 0:
            found = visit(name)
            if found:
                return found
    return []


def _topological_order(views: dict[str, dict[str, Any]]) -> list[str]:
    edges: dict[str, list[str]] = {}
    out_degree: dict[str, int] = {name: 0 for name in views}
    reverse: dict[str, list[str]] = {name: [] for name in views}
    for name, item in views.items():
        dependencies = [
            dependency
            for dependency in item["dependencies"]
            if dependency in views
        ]
        edges[name] = dependencies
        out_degree[name] = len(dependencies)
        for dependency in dependencies:
            reverse[dependency].append(name)
        if name in dependencies:
            raise BusinessError(
                E_DEPENDENCY_SELF,
                f"视图存在自依赖: {name}",
                view=name,
                node_id="dependency",
                dependency_path=[name, name],
            )
    ready = [name for name, degree in out_degree.items() if degree == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        name = heapq.heappop(ready)
        order.append(name)
        for downstream in sorted(reverse[name]):
            out_degree[downstream] -= 1
            if out_degree[downstream] == 0:
                heapq.heappush(ready, downstream)
    if len(order) != len(views):
        cycle = _find_cycle(edges)
        raise BusinessError(
            E_DEPENDENCY_CYCLE,
            "视图依赖图存在环",
            view=cycle[0] if cycle else None,
            node_id="dependency",
            dependency_path=cycle,
        )
    return order


def build_view_model(
    *,
    sources: dict[str, list[str]],
    sql_map: dict[str, str],
    declared_upstreams: dict[str, list[str]] | None = None,
    raw: dict[str, Any] | None = None,
    source_path: str | None = None,
) -> DefinitionSet:
    """Validate and compile a complete set of view definitions."""

    declared_upstreams = declared_upstreams or {}
    base_names = set(sources)
    view_names = set(sql_map)
    all_names = base_names | view_names
    definitions: dict[str, dict[str, Any]] = {}
    for name in sorted(view_names):
        if not name:
            raise BusinessError(E_VIEW_NAME_EMPTY, "视图名不能为空", view=name)
        if not valid_identifier(name):
            raise BusinessError(E_VIEW_NAME_INVALID, f"视图名不合法: {name}", view=name)
        sql = sql_map[name]
        if not isinstance(sql, str) or not sql.strip():
            raise BusinessError(E_SQL_SYNTAX, "SQL 文本为空", view=name, node_id="sql")
        inferred = extract_sql_sources(sql, view=name)
        for source in inferred:
            if source not in all_names:
                raise BusinessError(
                    E_DEPENDENCY_MISSING,
                    f"依赖上游不存在: {source}",
                    view=name,
                    node_id="scan",
                    dependency=source,
                )
        declared = list(declared_upstreams.get(name, []))
        for dependency in declared:
            if dependency not in all_names:
                raise BusinessError(
                    E_DEPENDENCY_MISSING,
                    f"声明的依赖上游不存在: {dependency}",
                    view=name,
                    node_id="dependency",
                    dependency=dependency,
                )
            if dependency == name:
                raise BusinessError(
                    E_DEPENDENCY_SELF,
                    f"视图存在自依赖: {name}",
                    view=name,
                    node_id="dependency",
                    dependency_path=[name, name],
                )
        view_dependencies = [
            dependency
            for dependency in dedupe_preserving_order(inferred + declared)
            if dependency in view_names
        ]
        definitions[name] = {
            "sql": normalize_sql(sql).rstrip(";").strip(),
            "inferred": inferred,
            "declared": declared,
            "dependencies": view_dependencies,
        }

    order = _topological_order(definitions)
    schemas = {name: list(columns) for name, columns in sources.items()}
    compiled: dict[str, ViewDefinition] = {}
    for name in order:
        item = definitions[name]
        try:
            plan = parse_sql_plan(
                name,
                item["sql"],
                schemas,
                dependency_names=view_names,
            )
        except BusinessError as exc:
            if exc.view is None:
                exc.view = name
            raise
        columns = plan["output_columns"]
        typed_dependencies = plan["dependencies"]
        missing_inferred = [
            source
            for source in item["inferred"]
            if source in view_names and source not in typed_dependencies
        ]
        if missing_inferred:
            raise BusinessError(
                E_DEPENDENCY_DECLARED_MISMATCH,
                "推导依赖与计划依赖不一致",
                view=name,
                inferred=item["inferred"],
                plan_dependencies=typed_dependencies,
            )
        schemas[name] = columns
        compiled[name] = ViewDefinition(
            name=name,
            sql=item["sql"],
            declared_upstreams=item["declared"],
            dependencies=item["dependencies"],
            plan=plan,
            columns=columns,
            column_dependencies=plan["column_dependencies"],
            executable_sql=item["sql"],
        )
    return DefinitionSet(
        sources=sources,
        views=compiled,
        order=order,
        raw=raw or {},
        source_path=source_path,
    )


def load_view_definitions(path: str | Path) -> DefinitionSet:
    raw = require_dict(read_json_file(path), "view_definitions")
    sources = _normalize_sources(raw.get("sources", {}))
    views_value = raw.get("views")
    if views_value is None:
        raise BusinessError(E_INPUT_SCHEMA, "缺少 views", field="views")
    sql_map: dict[str, str] = {}
    declared: dict[str, list[str]] = {}
    if isinstance(views_value, dict):
        iterator: Iterable[tuple[str, Any]] = views_value.items()
        for name, value in iterator:
            item = require_dict(value, f"views.{name}", view=name)
            actual_name = item.get("name", name)
            if actual_name != name:
                raise BusinessError(
                    E_INPUT_SCHEMA,
                    "views 对象键与 name 不一致",
                    view=name,
                    field="name",
                )
            sql_map[name] = item.get("sql")
            declared[name] = _declared_upstreams(item, view=name)
    else:
        for index, value in enumerate(require_list(views_value, "views")):
            item = require_dict(value, f"views[{index}]")
            name = item.get("name")
            if not isinstance(name, str) or not name:
                raise BusinessError(E_VIEW_NAME_EMPTY, "视图名不能为空", edit_round=None)
            if name in sql_map:
                raise BusinessError(
                    E_VIEW_NAME_DUPLICATE,
                    f"视图名重复: {name}",
                    view=name,
                )
            sql_map[name] = item.get("sql")
            declared[name] = _declared_upstreams(item, view=name)
    return build_view_model(
        sources=sources,
        sql_map=sql_map,
        declared_upstreams=declared,
        raw=raw,
        source_path=str(path),
    )


def _normalize_dependency_entry(entry: Any, *, view: str) -> dict[str, str]:
    if isinstance(entry, str):
        if "." not in entry:
            raise BusinessError(
                E_INPUT_SCHEMA,
                "列依赖字符串必须为 source.column",
                view=view,
                value=entry,
            )
        source, column = entry.rsplit(".", 1)
        return {"source": source, "column": column}
    item = require_dict(entry, "column dependency", view=view)
    source = item.get("source", item.get("view"))
    column = item.get("column")
    if not isinstance(source, str) or not isinstance(column, str):
        raise BusinessError(
            E_INPUT_SCHEMA,
            "列依赖必须包含 source 与 column",
            view=view,
            value=entry,
        )
    return {"source": source, "column": column}


def normalize_declared_column_dependencies(
    value: Any,
    *,
    view: str,
) -> dict[str, list[dict[str, str]]]:
    if value is None:
        return {}
    mapping = require_dict(value, f"column_dependencies[{view}]", view=view)
    result: dict[str, list[dict[str, str]]] = {}
    for column, entries in mapping.items():
        if not isinstance(column, str) or not column:
            raise BusinessError(E_INPUT_SCHEMA, "依赖列名必须非空", view=view)
        normalized = [
            _normalize_dependency_entry(entry, view=view)
            for entry in require_list(entries, f"column_dependencies[{view}].{column}", view=view)
        ]
        deduped: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in normalized:
            key = (item["source"], item["column"])
            if key not in seen:
                seen.add(key)
                deduped.append(item)
        result[column] = deduped
    return result


def _dependency_set(
    dependencies: dict[str, list[dict[str, str]]],
    column: str,
) -> set[tuple[str, str]]:
    return {
        (item["source"], item["column"])
        for item in dependencies.get(column, [])
    }


def _validate_declared_plan_columns(
    *,
    view: str,
    plan: dict[str, Any],
    declared_columns: list[str] | None,
    declared_dependencies: dict[str, list[dict[str, str]]] | None,
    code: str,
) -> None:
    if declared_columns:
        if declared_columns != plan["output_columns"]:
            raise BusinessError(
                code,
                "声明的输出列与 SQL 计划不一致",
                view=view,
                declared_columns=declared_columns,
                plan_columns=plan["output_columns"],
            )
    if declared_dependencies:
        for column, expected in declared_dependencies.items():
            if column not in plan["output_columns"]:
                raise BusinessError(
                    code,
                    f"依赖摘要引用了不存在的输出列: {column}",
                    view=view,
                    column=column,
                )
            actual = _dependency_set(plan["column_dependencies"], column)
            expected_set = {(item["source"], item["column"]) for item in expected}
            if actual != expected_set:
                raise BusinessError(
                    code,
                    f"列依赖摘要不一致: {view}.{column}",
                    view=view,
                    column=column,
                    declared=sorted([list(item) for item in expected_set]),
                    actual=sorted([list(item) for item in actual]),
                )


def load_v1_snapshot(
    path: str | Path,
    definitions: DefinitionSet,
    *,
    base_clock_ms: int | None = None,
) -> Snapshot:
    raw = require_dict(read_json_file(path), "v1_snapshot")
    if raw.get("format_version", 1) != 1:
        raise BusinessError(E_INPUT_SCHEMA, "v1 快照 format_version 必须为 1", field="format_version")
    clock = require_nonnegative_int(
        raw.get("logical_clock_ms"),
        "logical_clock_ms",
        code=E_LOGIC_CLOCK_INVALID,
    )
    generation = require_nonnegative_int(
        raw.get("snapshot_generation", 1),
        "snapshot_generation",
        code=E_PLAN_GENERATION_REGRESSION,
    )
    injected = raw.get("injected_clock_ms", clock)
    injected = require_nonnegative_int(
        injected,
        "injected_clock_ms",
        code=E_LOGIC_CLOCK_INVALID,
    )
    effective_base = injected if base_clock_ms is None else require_nonnegative_int(
        base_clock_ms,
        "base_clock_ms",
        code=E_LOGIC_CLOCK_INVALID,
    )
    views_value = require_list(raw.get("views", []), "v1.views")
    schemas = definitions.source_schemas()
    snapshot_views: list[SnapshotView] = []
    names: set[str] = set()
    for index, value in enumerate(views_value):
        item = require_dict(value, f"v1.views[{index}]")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise BusinessError(E_VIEW_NAME_EMPTY, "v1 视图名不能为空", view=name)
        if name in names:
            raise BusinessError(E_VIEW_NAME_DUPLICATE, f"v1 视图名重复: {name}", view=name)
        names.add(name)
        if name not in definitions.views:
            raise BusinessError(
                E_VIEW_MISSING,
                f"v1 视图未在定义集合中声明: {name}",
                view=name,
            )
        sql = item.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise BusinessError(E_SQL_SYNTAX, "v1 视图 SQL 为空", view=name, node_id="sql")
        generated_seconds = require_nonnegative_int(
            item.get("generated_seconds"),
            f"v1.views[{index}].generated_seconds",
            code=E_V1_TIME_INVALID,
            view=name,
        )
        absolute_ms = effective_base + generated_seconds * 1000
        try:
            plan = parse_sql_plan(
                name,
                normalize_sql(sql).rstrip(";").strip(),
                schemas,
                dependency_names=definitions.view_names,
            )
        except BusinessError as exc:
            if exc.view is None:
                exc.view = name
            exc.edit_round = None
            raise
        snapshot_views.append(
            SnapshotView(
                name=name,
                sql=normalize_sql(sql).rstrip(";").strip(),
                source_version="v1",
                clock_ms=absolute_ms,
                plan_generation=0,
                original_time={
                    "kind": "relative_seconds",
                    "relative_seconds": generated_seconds,
                    "snapshot_logical_clock_ms": clock,
                },
                conversion={
                    "base_ms": effective_base,
                    "scale": 1000,
                    "rounding": "floor",
                    "relative_seconds": generated_seconds,
                    "absolute_ms": absolute_ms,
                },
                plan=plan,
                declared_columns=list(plan["output_columns"]),
                declared_column_dependencies=plan["column_dependencies"],
                sample_order=index,
            )
        )

    column_map = require_list(raw.get("column_map", []), "v1.column_map")
    declared_by_view: dict[str, dict[str, set[tuple[str, str]]]] = {}
    for index, value in enumerate(column_map):
        item = require_dict(value, f"v1.column_map[{index}]")
        view = item.get("view")
        column = item.get("column")
        upstream_view = item.get("upstream_view", item.get("upstream"))
        upstream_column = item.get("upstream_column")
        generated_seconds = require_nonnegative_int(
            item.get("generated_seconds", 0),
            f"v1.column_map[{index}].generated_seconds",
            code=E_V1_TIME_INVALID,
            view=view if isinstance(view, str) else None,
        )
        if view not in definitions.views:
            raise BusinessError(
                E_V1_COLUMN_UNKNOWN,
                f"v1 列映射引用未声明视图: {view}",
                view=view if isinstance(view, str) else None,
                column=column,
                map_index=index,
            )
        if column not in definitions.views[view].columns:
            raise BusinessError(
                E_V1_COLUMN_UNKNOWN,
                f"v1 列映射引用未声明列: {view}.{column}",
                view=view,
                column=column,
                map_index=index,
            )
        if not isinstance(upstream_view, str) or upstream_view not in schemas:
            raise BusinessError(
                E_V1_COLUMN_UNKNOWN,
                f"v1 列映射引用未知上游: {upstream_view}",
                view=view,
                column=column,
                map_index=index,
            )
        if not isinstance(upstream_column, str) or upstream_column not in schemas[upstream_view]:
            raise BusinessError(
                E_V1_COLUMN_UNKNOWN,
                f"v1 列映射引用未知上游列: {upstream_view}.{upstream_column}",
                view=view,
                column=column,
                upstream_view=upstream_view,
                upstream_column=upstream_column,
                map_index=index,
            )
        declared_by_view.setdefault(view, {}).setdefault(column, set()).add(
            (upstream_view, upstream_column)
        )

    for snapshot_view in snapshot_views:
        declared = declared_by_view.get(snapshot_view.name)
        if not declared:
            continue
        for column, expected in declared.items():
            actual = {
                (item["source"], item["column"])
                for item in snapshot_view.plan["column_dependencies"].get(column, [])
            }
            if not expected.issubset(actual):
                raise BusinessError(
                    E_V1_COLUMN_UNKNOWN,
                    f"v1 列映射无法追溯到计划依赖: {snapshot_view.name}.{column}",
                    view=snapshot_view.name,
                    column=column,
                    declared=sorted([list(item) for item in expected]),
                    actual=sorted([list(item) for item in actual]),
                )

    try:
        source_hash = sha256_bytes(Path(path).read_bytes())
    except OSError as exc:
        raise BusinessError(E_INPUT_SCHEMA, "无法读取 v1 快照", path=str(path), error=str(exc)) from exc
    return Snapshot(
        format_version=1,
        logical_clock_ms=clock,
        snapshot_generation=generation,
        views=snapshot_views,
        source_path=str(path),
        source_hashes={str(path): source_hash},
    )


def _connect_readonly(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BusinessError(E_INPUT_SCHEMA, "无法打开 v2 SQLite 元数据库", path=str(path), error=str(exc)) from exc
    connection.row_factory = sqlite3.Row
    return connection


def load_v2_snapshot(
    shards_dir: str | Path,
    meta_db: str | Path,
    definitions: DefinitionSet,
) -> Snapshot:
    shard_root = Path(shards_dir).resolve()
    db_path = Path(meta_db)
    connection = _connect_readonly(db_path)
    try:
        try:
            meta_rows = connection.execute("SELECT key, value FROM meta").fetchall()
        except sqlite3.Error as exc:
            raise BusinessError(
                E_V2_META_MISMATCH,
                "v2 元数据库缺少 meta 表",
                path=str(db_path),
                error=str(exc),
            ) from exc
        meta = {row["key"]: row["value"] for row in meta_rows}
        if str(meta.get("format_version")) != "2":
            raise BusinessError(
                E_V2_META_MISMATCH,
                "v2 元数据库 format_version 必须为 2",
                format_version=meta.get("format_version"),
            )
        try:
            current_generation = int(meta["current_plan_generation"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BusinessError(
                E_PLAN_GENERATION_REGRESSION,
                "v2 current_plan_generation 缺失或非法",
                value=meta.get("current_plan_generation"),
            ) from exc
        if current_generation < 0:
            raise BusinessError(
                E_PLAN_GENERATION_REGRESSION,
                "v2 current_plan_generation 不能为负",
                value=current_generation,
            )
        try:
            shard_rows = connection.execute(
                "SELECT shard_id, file_name, sha256, plan_generation, "
                "logical_clock_ms, generated_at_ms FROM shards "
                "ORDER BY plan_generation, shard_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise BusinessError(
                E_V2_META_MISMATCH,
                "v2 元数据库缺少 shards 表或字段",
                error=str(exc),
            ) from exc
        if not shard_rows:
            raise BusinessError(E_V2_META_MISMATCH, "v2 未声明任何分片")
        previous_generation = -1
        source_hashes: dict[str, str] = {}
        snapshot_views: list[SnapshotView] = []
        schemas = definitions.source_schemas()
        view_names: set[tuple[str, int]] = set()
        expected_view_meta: dict[tuple[str, int], dict[str, Any]] = {}
        for shard_row in shard_rows:
            shard_generation = int(shard_row["plan_generation"])
            if shard_generation <= previous_generation:
                raise BusinessError(
                    E_PLAN_GENERATION_REGRESSION,
                    "v2 分片计划代数未单调递增",
                    shard_id=shard_row["shard_id"],
                    previous_generation=previous_generation,
                    plan_generation=shard_generation,
                )
            previous_generation = shard_generation
            file_name = shard_row["file_name"]
            relative = Path(file_name)
            if relative.is_absolute() or ".." in relative.parts:
                raise BusinessError(
                    E_V2_META_MISMATCH,
                    "v2 分片路径必须位于 shards 目录内",
                    shard_id=shard_row["shard_id"],
                    file_name=file_name,
                )
            shard_path = (shard_root / relative).resolve()
            try:
                shard_path.relative_to(shard_root)
            except ValueError as exc:
                raise BusinessError(
                    E_V2_META_MISMATCH,
                    "v2 分片路径越界",
                    shard_id=shard_row["shard_id"],
                    file_name=file_name,
                ) from exc
            try:
                shard_bytes = shard_path.read_bytes()
            except OSError as exc:
                raise BusinessError(
                    E_V2_META_MISMATCH,
                    "v2 分片文件不可读",
                    shard_id=shard_row["shard_id"],
                    path=str(shard_path),
                    error=str(exc),
                ) from exc
            actual_hash = sha256_bytes(shard_bytes)
            expected_hash = shard_row["sha256"]
            if actual_hash != expected_hash:
                raise BusinessError(
                    E_SHARD_HASH_MISMATCH,
                    "v2 分片哈希不匹配",
                    shard_id=shard_row["shard_id"],
                    path=str(shard_path),
                    expected=expected_hash,
                    actual=actual_hash,
                )
            source_hashes[str(shard_path)] = actual_hash
            try:
                shard = require_dict(
                    json.loads(shard_bytes.decode("utf-8")),
                    f"v2 shard {shard_row['shard_id']}",
                )
            except (UnicodeDecodeError, ValueError) as exc:
                raise BusinessError(
                    E_INPUT_SCHEMA,
                    "v2 分片 JSON 非法",
                    shard_id=shard_row["shard_id"],
                    path=str(shard_path),
                    error=str(exc),
                ) from exc
            if shard.get("format_version") != 2:
                raise BusinessError(
                    E_INPUT_SCHEMA,
                    "v2 分片 format_version 必须为 2",
                    shard_id=shard_row["shard_id"],
                )
            if shard.get("shard_id") != shard_row["shard_id"]:
                raise BusinessError(
                    E_V2_META_MISMATCH,
                    "v2 分片 ID 与元数据库不一致",
                    shard_id=shard_row["shard_id"],
                    shard_id_in_file=shard.get("shard_id"),
                )
            if int(shard.get("plan_generation", -1)) != shard_generation:
                raise BusinessError(
                    E_PLAN_GENERATION_REGRESSION,
                    "v2 分片计划代数与元数据库不一致",
                    shard_id=shard_row["shard_id"],
                    meta_generation=shard_generation,
                    file_generation=shard.get("plan_generation"),
                )
            meta_shard_clock = require_nonnegative_int(
                shard_row["logical_clock_ms"],
                "v2.shards.logical_clock_ms",
                code=E_LOGIC_CLOCK_INVALID,
                view=None,
            )
            meta_generated_at = require_nonnegative_int(
                shard_row["generated_at_ms"],
                "v2.shards.generated_at_ms",
                code=E_LOGIC_CLOCK_INVALID,
                view=None,
            )
            file_shard_clock = require_nonnegative_int(
                shard.get("logical_clock_ms", meta_shard_clock),
                "v2.logical_clock_ms",
                code=E_LOGIC_CLOCK_INVALID,
            )
            if file_shard_clock != meta_shard_clock:
                raise BusinessError(
                    E_V2_META_MISMATCH,
                    "v2 分片逻辑时钟与元数据库不一致",
                    shard_id=shard_row["shard_id"],
                    meta_value=meta_shard_clock,
                    file_value=file_shard_clock,
                )
            file_generated_at = require_nonnegative_int(
                shard.get("generated_at_ms", meta_generated_at),
                "v2.generated_at_ms",
                code=E_LOGIC_CLOCK_INVALID,
            )
            if file_generated_at != meta_generated_at:
                raise BusinessError(
                    E_V2_META_MISMATCH,
                    "v2 分片生成毫秒与元数据库不一致",
                    shard_id=shard_row["shard_id"],
                    meta_value=meta_generated_at,
                    file_value=file_generated_at,
                )
            shard_views = require_list(shard.get("views", []), "v2.views")
            for index, value in enumerate(shard_views):
                item = require_dict(value, f"v2.{shard_row['shard_id']}.views[{index}]")
                name = item.get("canonical_name", item.get("name"))
                if not isinstance(name, str) or not name:
                    raise BusinessError(
                        E_VIEW_NAME_EMPTY,
                        "v2 视图规范名不能为空",
                        shard_id=shard_row["shard_id"],
                    )
                if name not in definitions.views:
                    raise BusinessError(
                        E_VIEW_MISSING,
                        f"v2 视图未在定义集合中声明: {name}",
                        view=name,
                        shard_id=shard_row["shard_id"],
                    )
                key = (name, shard_generation)
                if key in view_names:
                    raise BusinessError(
                        E_VIEW_NAME_DUPLICATE,
                        f"v2 同一分片代数内视图重复: {name}",
                        view=name,
                        shard_id=shard_row["shard_id"],
                    )
                view_names.add(key)
                sql = item.get("sql")
                if not isinstance(sql, str) or not sql.strip():
                    raise BusinessError(E_SQL_SYNTAX, "v2 视图 SQL 为空", view=name, node_id="sql")
                generated_at = require_nonnegative_int(
                    item.get("generated_at_ms", shard_row["generated_at_ms"]),
                    "v2.generated_at_ms",
                    code=E_LOGIC_CLOCK_INVALID,
                    view=name,
                )
                clock_ms = require_nonnegative_int(
                    item.get("logical_clock_ms", generated_at),
                    "v2.logical_clock_ms",
                    code=E_LOGIC_CLOCK_INVALID,
                    view=name,
                )
                declared_columns = item.get("columns")
                if declared_columns is not None:
                    declared_columns = _normalize_columns(
                        declared_columns,
                        field_name=f"v2.{name}.columns",
                    )
                declared_dependencies = normalize_declared_column_dependencies(
                    item.get("column_dependencies"),
                    view=name,
                )
                try:
                    plan = parse_sql_plan(
                        name,
                        normalize_sql(sql).rstrip(";").strip(),
                        schemas,
                        dependency_names=definitions.view_names,
                    )
                except BusinessError as exc:
                    if exc.view is None:
                        exc.view = name
                    raise
                _validate_declared_plan_columns(
                    view=name,
                    plan=plan,
                    declared_columns=declared_columns,
                    declared_dependencies=declared_dependencies,
                    code=E_V2_META_MISMATCH,
                )
                snapshot_views.append(
                    SnapshotView(
                        name=name,
                        sql=normalize_sql(sql).rstrip(";").strip(),
                        source_version="v2",
                        clock_ms=clock_ms,
                        plan_generation=shard_generation,
                        original_time={
                            "kind": "absolute_ms",
                            "value": generated_at,
                            "shard_generated_at_ms": shard_row["generated_at_ms"],
                        },
                        conversion=None,
                        plan=plan,
                        declared_columns=declared_columns or list(plan["output_columns"]),
                        declared_column_dependencies=declared_dependencies,
                        shard_id=shard_row["shard_id"],
                        sample_order=index,
                    )
                )
                expected_view_meta[(name, shard_generation)] = {
                    "canonical_name": name,
                    "plan_generation": shard_generation,
                    "logical_clock_ms": clock_ms,
                    "generated_at_ms": generated_at,
                    "column_dependencies_json": canonical_json(
                        declared_dependencies
                    ),
                    "tombstone": 0,
                    "shard_id": shard_row["shard_id"],
                }

        try:
            tombstone_rows = connection.execute(
                "SELECT view_name, plan_generation, logical_clock_ms, generated_at_ms, reason "
                "FROM tombstones ORDER BY plan_generation, view_name"
            ).fetchall()
        except sqlite3.Error as exc:
            raise BusinessError(
                E_V2_META_MISMATCH,
                "v2 元数据库缺少 tombstones 表或字段",
                error=str(exc),
            ) from exc
        tombstones: list[SnapshotView] = []
        for row in tombstone_rows:
            name = row["view_name"]
            generation = require_nonnegative_int(
                row["plan_generation"],
                "v2.tombstone.plan_generation",
                code=E_PLAN_GENERATION_REGRESSION,
                view=name,
            )
            if generation > current_generation:
                raise BusinessError(
                    E_PLAN_GENERATION_REGRESSION,
                    "v2 墓碑计划代数超过当前代数",
                    view=name,
                    plan_generation=generation,
                    current_generation=current_generation,
                )
            clock_ms = require_nonnegative_int(
                row["logical_clock_ms"],
                "v2.tombstone.logical_clock_ms",
                code=E_LOGIC_CLOCK_INVALID,
                view=name,
            )
            generated_at = require_nonnegative_int(
                row["generated_at_ms"],
                "v2.tombstone.generated_at_ms",
                code=E_LOGIC_CLOCK_INVALID,
                view=name,
            )
            tombstones.append(
                SnapshotView(
                    name=name,
                    sql="",
                    source_version="v2",
                    clock_ms=clock_ms,
                    plan_generation=generation,
                    original_time={
                        "kind": "absolute_ms",
                        "value": generated_at,
                        "tombstone_reason": row["reason"],
                    },
                    conversion=None,
                    plan={},
                    sample_order=0,
                )
            )
            expected_view_meta[(name, generation)] = {
                "canonical_name": name,
                "plan_generation": generation,
                "logical_clock_ms": clock_ms,
                "generated_at_ms": generated_at,
                "column_dependencies_json": "{}",
                "tombstone": 1,
                "shard_id": None,
            }
        try:
            view_meta_rows = connection.execute(
                "SELECT canonical_name, plan_generation, logical_clock_ms, "
                "generated_at_ms, column_dependencies_json, tombstone, shard_id "
                "FROM view_meta ORDER BY canonical_name, plan_generation"
            ).fetchall()
        except sqlite3.Error as exc:
            raise BusinessError(
                E_V2_META_MISMATCH,
                "v2 元数据库缺少 view_meta 表或字段",
                error=str(exc),
            ) from exc
        seen_view_meta: set[tuple[str, int]] = set()
        for row in view_meta_rows:
            key = (row["canonical_name"], row["plan_generation"])
            expected = expected_view_meta.get(key)
            if expected is None:
                raise BusinessError(
                    E_V2_META_MISMATCH,
                    "view_meta 中存在分片未声明的视图代数",
                    view=row["canonical_name"],
                    plan_generation=row["plan_generation"],
                )
            if key in seen_view_meta:
                raise BusinessError(
                    E_V2_META_MISMATCH,
                    "view_meta 中视图代数重复",
                    view=row["canonical_name"],
                    plan_generation=row["plan_generation"],
                )
            seen_view_meta.add(key)
            try:
                actual_dependencies = normalize_declared_column_dependencies(
                    json.loads(row["column_dependencies_json"]),
                    view=row["canonical_name"],
                )
            except (json.JSONDecodeError, BusinessError) as exc:
                raise BusinessError(
                    E_V2_META_MISMATCH,
                    "view_meta 列依赖摘要 JSON 非法",
                    view=row["canonical_name"],
                    plan_generation=row["plan_generation"],
                ) from exc
            comparisons = {
                "logical_clock_ms": row["logical_clock_ms"],
                "generated_at_ms": row["generated_at_ms"],
                "tombstone": int(row["tombstone"]),
                "shard_id": row["shard_id"],
            }
            for field_name, actual in comparisons.items():
                if expected[field_name] is None:
                    continue
                if actual != expected[field_name]:
                    raise BusinessError(
                        E_V2_META_MISMATCH,
                        f"view_meta 与分片不一致: {field_name}",
                        view=row["canonical_name"],
                        plan_generation=row["plan_generation"],
                        field=field_name,
                        meta_value=actual,
                        shard_value=expected[field_name],
                    )
            if canonical_json(actual_dependencies) != expected["column_dependencies_json"]:
                raise BusinessError(
                    E_V2_META_MISMATCH,
                    "view_meta 列依赖摘要与分片不一致",
                    view=row["canonical_name"],
                    plan_generation=row["plan_generation"],
                )
        missing_view_meta = sorted(set(expected_view_meta) - seen_view_meta)
        if missing_view_meta:
            raise BusinessError(
                E_V2_META_MISMATCH,
                "view_meta 缺少分片声明的视图代数",
                missing=[
                    {"view": name, "plan_generation": generation}
                    for name, generation in missing_view_meta
                ],
            )
        max_generation = max(
            [previous_generation]
            + [tombstone.plan_generation for tombstone in tombstones]
        )
        if current_generation < max_generation:
            raise BusinessError(
                E_PLAN_GENERATION_REGRESSION,
                "v2 current_plan_generation 小于已声明分片/墓碑代数",
                current_generation=current_generation,
                max_generation=max_generation,
            )
    finally:
        connection.close()
    try:
        db_hash = sha256_bytes(db_path.read_bytes())
    except OSError:
        db_hash = ""
    source_hashes[str(db_path)] = db_hash
    logical_clock = require_nonnegative_int(
        int(meta.get("logical_clock_ms", current_generation)),
        "v2.meta.logical_clock_ms",
        code=E_LOGIC_CLOCK_INVALID,
    )
    return Snapshot(
        format_version=2,
        logical_clock_ms=logical_clock,
        snapshot_generation=current_generation,
        views=snapshot_views,
        tombstones=tombstones,
        source_path=str(db_path),
        source_hashes=source_hashes,
    )


def _winner_reason(
    winner: SnapshotView,
    candidates: list[SnapshotView],
) -> str:
    others = [candidate for candidate in candidates if candidate is not winner]
    if not others:
        return "only_candidate"
    if any(candidate.clock_ms < winner.clock_ms for candidate in others):
        return "logical_clock"
    if any(
        candidate.clock_ms == winner.clock_ms
        and candidate.source_version != winner.source_version
        for candidate in others
    ):
        return "source_priority"
    if any(
        candidate.clock_ms == winner.clock_ms
        and candidate.source_version == winner.source_version
        and candidate.plan_generation < winner.plan_generation
        for candidate in others
    ):
        return "plan_generation"
    return "stable_order"


def merge_snapshots(
    definitions: DefinitionSet,
    v1: Snapshot,
    v2: Snapshot,
    *,
    base_clock_ms: int | None = None,
) -> dict[str, Any]:
    grouped: dict[str, list[SnapshotView]] = {}
    for candidate in [*v1.views, *v2.views, *v2.tombstones]:
        grouped.setdefault(candidate.name, []).append(candidate)

    selected_views: dict[str, SnapshotView] = {}
    tombstones: list[dict[str, Any]] = []
    decisions: dict[str, dict[str, Any]] = {}
    for name in sorted(grouped):
        candidates = grouped[name]
        winner = max(
            candidates,
            key=lambda item: (
                item.clock_ms,
                2 if item.source_version == "v2" else 1,
                item.plan_generation,
                item.sample_order,
            ),
        )
        reason = _winner_reason(winner, candidates)
        decision_candidates = []
        for candidate in sorted(
            candidates,
            key=lambda item: (
                item.clock_ms,
                item.source_version,
                item.plan_generation,
                item.sample_order,
            ),
        ):
            decision_candidates.append(
                {
                    "source_version": candidate.source_version,
                    "logical_clock_ms": candidate.clock_ms,
                    "plan_generation": candidate.plan_generation,
                    "status": "tombstone" if candidate in v2.tombstones else "active",
                    "selected": candidate is winner,
                }
            )
        decision = {
            "rule": "logical_clock_then_source_priority_then_plan_generation",
            "reason": reason,
            "winner": winner.source_version,
            "candidates": decision_candidates,
        }
        if winner in v2.tombstones:
            tombstones.append(
                {
                    "name": name,
                    "source_version": winner.source_version,
                    "logical_clock_ms": winner.clock_ms,
                    "plan_generation": winner.plan_generation,
                    "original_time": winner.original_time,
                    "decision": decision,
                }
            )
        else:
            selected_views[name] = winner
        decisions[name] = decision

    sql_map = {name: candidate.sql for name, candidate in selected_views.items()}
    declared_map = {
        name: candidate.declared_dependencies
        for name, candidate in selected_views.items()
    }
    merged_model = build_view_model(
        sources=definitions.sources,
        sql_map=sql_map,
        declared_upstreams=declared_map,
        raw={"merged": True},
        source_path=None,
    )
    result_views: list[dict[str, Any]] = []
    for name in sorted(merged_model.views):
        view = merged_model.views[name]
        candidate = selected_views[name]
        _validate_declared_plan_columns(
            view=name,
            plan=view.plan,
            declared_columns=(
                candidate.declared_columns
                if candidate.source_version == "v2"
                else None
            ),
            declared_dependencies=(
                candidate.declared_column_dependencies
                if candidate.source_version == "v2"
                else None
            ),
            code=E_V2_META_MISMATCH,
        )
        result_views.append(
            {
                "name": name,
                "source_version": candidate.source_version,
                "source_plan_generation": candidate.plan_generation,
                "absolute_time_ms": candidate.clock_ms,
                "original_time": candidate.original_time,
                "conversion": candidate.conversion,
                "decision": decisions[name],
                "dependencies": view.dependencies,
                "declared_upstreams": view.declared_upstreams,
                "sql": view.executable_sql,
                "executable_sql": view.executable_sql,
                "columns": view.columns,
                "column_dependencies": view.column_dependencies,
                "plan": view.plan,
                "plan_summary": view.plan["plan_summary"],
                "shard_id": candidate.shard_id,
            }
        )
    all_clocks = [
        candidate.clock_ms
        for candidates in grouped.values()
        for candidate in candidates
    ]
    source_hashes = {**v1.source_hashes, **v2.source_hashes}
    fingerprint = sha256_json(
        {
            "source_hashes": source_hashes,
            "views": {
                name: {
                    "sql": view.executable_sql,
                    "plan_summary": view.plan["plan_summary"],
                }
                for name, view in merged_model.views.items()
            },
        }
    )
    return {
        "format": "gsb-merged-plan-v1",
        "snapshot_generation": 1,
        "logical_clock_ms": max(all_clocks + [v1.logical_clock_ms, v2.logical_clock_ms]),
        "v1_base_clock_ms": (
            v1.views[0].conversion["base_ms"]
            if v1.views and v1.views[0].conversion
            else base_clock_ms
        ),
        "source_fingerprint": fingerprint,
        "source_hashes": dict(sorted(source_hashes.items())),
        "order": merged_model.order,
        "views": result_views,
        "tombstones": sorted(tombstones, key=lambda item: item["name"]),
        "counts": {
            "active_views": len(result_views),
            "tombstones": len(tombstones),
            "v1_only": sum(1 for item in result_views if item["source_version"] == "v1"),
            "v2_selected": sum(1 for item in result_views if item["source_version"] == "v2"),
        },
    }


def load_and_merge_sample(
    sample_dir: str | Path,
    *,
    base_clock_ms: int | None = None,
) -> tuple[DefinitionSet, Snapshot, Snapshot, dict[str, Any]]:
    root = Path(sample_dir)
    definitions = load_view_definitions(root / "view_definitions.json")
    v1 = load_v1_snapshot(
        root / "v1_snapshot.json",
        definitions,
        base_clock_ms=base_clock_ms,
    )
    v2 = load_v2_snapshot(root / "v2_shards", root / "sqlite_meta.db", definitions)
    merged = merge_snapshots(definitions, v1, v2, base_clock_ms=base_clock_ms)
    return definitions, v1, v2, merged

