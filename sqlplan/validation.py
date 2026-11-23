from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .errors import BusinessError, ErrorCode
from .models import ViewDef
from .util import is_sha256, normalize_digest, read_json_file


VIEW_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_view_name(name: Any, details: Optional[Dict[str, Any]] = None) -> str:
    if not isinstance(name, str) or not VIEW_NAME_RE.match(name):
        payload = {"field": "name", "value": name}
        if details:
            payload.update(details)
        raise BusinessError(
            ErrorCode.E_BAD_VIEW,
            "视图名必须是合法的简单 SQL 标识符",
            payload,
        )
    return name


def parse_single_select(
    sql: Any,
    *,
    view_name: Optional[str] = None,
    edit_round: Optional[int] = None,
) -> exp.Query:
    if not isinstance(sql, str) or not sql.strip():
        raise BusinessError(
            ErrorCode.E_BAD_VIEW,
            "视图 SQL 必须是非空字符串",
            {"view": view_name, "edit_round": edit_round},
        )
    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except ParseError as exc:
        raise BusinessError(
            ErrorCode.E_SQL_PARSE,
            "SQL 语法解析失败",
            {
                "view": view_name,
                "edit_round": edit_round,
                "reason": str(exc),
            },
        ) from exc
    statements = [statement for statement in statements if statement is not None]
    if len(statements) != 1:
        raise BusinessError(
            ErrorCode.E_SQL_PARSE,
            "视图 SQL 必须恰好包含一个查询语句",
            {
                "view": view_name,
                "edit_round": edit_round,
                "statement_count": len(statements),
            },
        )
    statement = statements[0]
    if not isinstance(statement, exp.Query):
        raise BusinessError(
            ErrorCode.E_SQL_PARSE,
            "视图 SQL 必须是 SELECT 查询",
            {
                "view": view_name,
                "edit_round": edit_round,
                "statement_type": type(statement).__name__,
            },
        )
    return statement


def validate_view_dict(
    value: Mapping[str, Any],
    *,
    inherited_clock: Optional[int] = None,
    verify_digest: bool = True,
) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise BusinessError(ErrorCode.E_BAD_VIEW, "视图定义必须是 JSON 对象")

    name = validate_view_name(value.get("name"))
    sql = value.get("sql")
    parse_single_select(sql, view_name=name)

    upstream_raw = value.get("upstream", [])
    if upstream_raw is None:
        upstream_raw = []
    if not isinstance(upstream_raw, list):
        raise BusinessError(
            ErrorCode.E_BAD_VIEW,
            "upstream 必须是视图名数组",
            {"view": name, "field": "upstream"},
        )
    upstream: List[str] = []
    for item in upstream_raw:
        upstream.append(validate_view_name(item, {"view": name, "field": "upstream"}))
    if len(upstream) != len(set(upstream)):
        raise BusinessError(
            ErrorCode.E_BAD_VIEW,
            "upstream 不得包含重复视图",
            {"view": name, "upstream": upstream},
        )
    if name in upstream:
        raise BusinessError(
            ErrorCode.E_CYCLE,
            "视图不得依赖自身",
            {"view": name, "cycle": [name, name], "edit_round": None},
        )

    clock = value.get("logical_clock_ms", inherited_clock)
    if not _is_nonnegative_int(clock):
        raise BusinessError(
            ErrorCode.E_BAD_CLOCK,
            "logical_clock_ms 必须是非负整数毫秒戳",
            {"view": name, "value": clock},
        )

    digest = value.get("content_digest")
    if not isinstance(digest, str) or not digest.strip():
        raise BusinessError(
            ErrorCode.E_BAD_VIEW,
            "content_digest 必须是非空 sha256 摘要",
            {"view": name, "field": "content_digest"},
        )
    if not is_sha256(digest):
        raise BusinessError(
            ErrorCode.E_BAD_VIEW,
            "content_digest 不是合法 sha256 摘要",
            {"view": name, "content_digest": digest},
        )

    parsed = {
        "name": name,
        "sql": sql,
        "upstream": tuple(upstream),
        "logical_clock_ms": clock,
        "content_digest": digest,
    }
    if verify_digest:
        calculated = ViewDef(**parsed).calculated_content_digest()
        if normalize_digest(calculated) != normalize_digest(digest):
            raise BusinessError(
                ErrorCode.E_CONTENT_DIGEST_MISMATCH,
                "视图内容摘要与内容不一致",
                {
                    "view": name,
                    "declared": digest,
                    "calculated": calculated,
                },
            )
    return parsed


def validate_view_collection(
    data: Any,
    *,
    verify_digest: bool = True,
) -> Tuple[Dict[str, ViewDef], int]:
    if isinstance(data, list):
        entries = data
        base_clock: Optional[int] = None
    elif isinstance(data, Mapping):
        entries = data.get("views")
        base_clock = data.get("logical_clock_ms")
        if base_clock is not None and not _is_nonnegative_int(base_clock):
            raise BusinessError(
                ErrorCode.E_BAD_CLOCK,
                "视图集合的 logical_clock_ms 必须是非负整数",
                {"value": base_clock},
            )
    else:
        raise BusinessError(ErrorCode.E_BAD_VIEW, "视图集合必须是 JSON 对象或数组")
    if not isinstance(entries, list):
        raise BusinessError(
            ErrorCode.E_BAD_VIEW,
            "views 必须是数组",
            {"actual_type": type(entries).__name__},
        )

    views: Dict[str, ViewDef] = {}
    for index, entry in enumerate(entries):
        try:
            parsed = validate_view_dict(
                entry,
                inherited_clock=base_clock,
                verify_digest=verify_digest,
            )
        except BusinessError as exc:
            exc.details.setdefault("view_index", index)
            raise
        name = parsed["name"]
        if name in views:
            raise BusinessError(
                ErrorCode.E_DUPLICATE_VIEW,
                "视图名必须唯一",
                {"view": name, "view_index": index},
            )
        views[name] = ViewDef(**parsed)

    validate_dependencies(views)
    validate_sql_source_declarations(views)
    watermark = max(
        [base_clock or 0] + [view.logical_clock_ms for view in views.values()]
    )
    return views, watermark


def load_views(path: str | Path, *, verify_digest: bool = True):
    return validate_view_collection(read_json_file(path), verify_digest=verify_digest)


def validate_dependencies(views: Mapping[str, ViewDef]) -> None:
    for view in views.values():
        for upstream in view.upstream:
            if upstream not in views:
                raise BusinessError(
                    ErrorCode.E_UPSTREAM_NOT_FOUND,
                    "视图声明的上游视图不存在",
                    {"view": view.name, "upstream": upstream},
                )
    cycle = find_cycle(views)
    if cycle:
        raise BusinessError(
            ErrorCode.E_CYCLE,
            "视图依赖图存在循环",
            {"view": cycle[0], "cycle": cycle},
        )


def validate_sql_source_declarations(views: Mapping[str, ViewDef]) -> None:
    view_names = set(views)
    for view in views.values():
        query = parse_single_select(view.sql, view_name=view.name)
        referenced_views = {
            table.name
            for table in query.find_all(exp.Table)
            if table.name in view_names
        }
        undeclared = sorted(referenced_views - set(view.upstream))
        if undeclared:
            raise BusinessError(
                ErrorCode.E_BAD_VIEW,
                "SQL 引用的视图未在 upstream 中声明",
                {
                    "view": view.name,
                    "undeclared_view_references": undeclared,
                    "declared_upstream": list(view.upstream),
                },
            )


def find_cycle(views: Mapping[str, ViewDef]) -> List[str]:
    state: Dict[str, int] = {}
    stack: List[str] = []
    position: Dict[str, int] = {}

    def visit(name: str) -> Optional[List[str]]:
        state[name] = 1
        position[name] = len(stack)
        stack.append(name)
        for upstream in sorted(views[name].upstream):
            if upstream not in views:
                continue
            if state.get(upstream, 0) == 0:
                cycle = visit(upstream)
                if cycle:
                    return cycle
            elif state.get(upstream) == 1:
                start = position[upstream]
                return stack[start:] + [upstream]
        stack.pop()
        position.pop(name, None)
        state[name] = 2
        return None

    for name in sorted(views):
        if state.get(name, 0) == 0:
            cycle = visit(name)
            if cycle:
                return cycle
    return []


def topological_order(views: Mapping[str, ViewDef]) -> List[str]:
    indegree = {name: 0 for name in views}
    downstream: Dict[str, List[str]] = {name: [] for name in views}
    for name, view in views.items():
        for upstream in view.upstream:
            if upstream in views:
                downstream[upstream].append(name)
                indegree[name] += 1
    ready = sorted(name for name, degree in indegree.items() if degree == 0)
    result: List[str] = []
    while ready:
        name = ready.pop(0)
        result.append(name)
        for child in sorted(downstream[name]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(result) != len(views):
        cycle = find_cycle(views)
        raise BusinessError(
            ErrorCode.E_CYCLE,
            "视图依赖图存在循环",
            {"view": cycle[0] if cycle else None, "cycle": cycle},
        )
    return result


def downstream_map(views: Mapping[str, ViewDef]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {name: [] for name in views}
    for name, view in views.items():
        for upstream in view.upstream:
            result.setdefault(upstream, []).append(name)
    for children in result.values():
        children.sort()
    return result


def find_paths(
    downstream: Mapping[str, Sequence[str]],
    start: str,
    targets: Iterable[str],
) -> Dict[str, List[str]]:
    targets_set = set(targets)
    result: Dict[str, List[str]] = {}
    queue: List[List[str]] = [[start]]
    seen_depth = {start: 0}
    while queue:
        path = queue.pop(0)
        current = path[-1]
        if current in targets_set:
            result[current] = path
            continue
        for child in downstream.get(current, []):
            depth = len(path)
            if child not in seen_depth or seen_depth[child] > depth:
                seen_depth[child] = depth
                queue.append(path + [child])
    return result
