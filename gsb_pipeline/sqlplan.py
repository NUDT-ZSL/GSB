"""Deterministic SQL logical-plan construction backed by sqlparse.

sqlparse performs statement validation and comment handling.  The structural
parser below intentionally supports a compact, explicit SQL subset so that the
same input always produces byte-identical logical plans.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import sqlparse

from .errors import (
    BusinessError,
    E_COLUMN_UNKNOWN,
    E_DEPENDENCY_MISSING,
    E_SQL_AMBIGUOUS_COLUMN,
    E_SQL_DUPLICATE_COLUMN,
    E_SQL_SYNTAX,
    E_SQL_UNSUPPORTED,
)
from .utils import (
    dedupe_preserving_order,
    mask_sql_literals,
    normalize_sql,
    sha256_json,
    split_by_top_level_keyword,
    split_top_level,
    top_level_words,
    valid_identifier,
)


_SQL_KEYWORDS = {
    "ALL",
    "AND",
    "AS",
    "ASC",
    "BETWEEN",
    "BY",
    "CASE",
    "CAST",
    "COLLATE",
    "CROSS",
    "DESC",
    "DISTINCT",
    "ELSE",
    "END",
    "EXCEPT",
    "EXISTS",
    "FALSE",
    "FROM",
    "FULL",
    "GROUP",
    "HAVING",
    "IN",
    "INNER",
    "INTERSECT",
    "IS",
    "JOIN",
    "LEFT",
    "LIKE",
    "LIMIT",
    "NATURAL",
    "NOT",
    "NULL",
    "OFFSET",
    "ON",
    "OR",
    "ORDER",
    "OUTER",
    "RIGHT",
    "SELECT",
    "THEN",
    "TRUE",
    "UNION",
    "USING",
    "WHEN",
    "WHERE",
    "WITH",
}

_TYPE_WORDS = {
    "BIGINT",
    "BLOB",
    "BOOLEAN",
    "CHAR",
    "DATE",
    "DATETIME",
    "DECIMAL",
    "DOUBLE",
    "FLOAT",
    "INT",
    "INTEGER",
    "NUMERIC",
    "REAL",
    "TEXT",
    "TIMESTAMP",
    "VARCHAR",
}

_AGGREGATES = {"AVG", "COUNT", "MAX", "MIN", "SUM"}
_JOIN_PATTERN = re.compile(
    r"\b(?P<kind>(?:(?:NATURAL|LEFT|RIGHT|FULL|INNER|CROSS)\s+)*"
    r"(?:OUTER\s+)?JOIN)\b",
    re.IGNORECASE,
)
_TABLE_PATTERN = re.compile(
    r"^\s*(?P<table>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)"
    r"(?:\s+(?:AS\s+)?(?P<alias>[A-Za-z_][A-Za-z0-9_]*))?\s*$",
    re.IGNORECASE,
)
_QUALIFIED_PATTERN = re.compile(
    r"\b(?P<qualifier>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*"
    r"(?P<column>\*|[A-Za-z_][A-Za-z0-9_]*)\b"
)
_WORD_PATTERN = re.compile(r"\b(?P<word>[A-Za-z_][A-Za-z0-9_]*)\b")
_ALIAS_AS_PATTERN = re.compile(
    r"^(?P<expression>.+?)\s+AS\s+(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\s*$",
    re.IGNORECASE,
)
_ALIAS_IMPLICIT_PATTERN = re.compile(
    r"^(?P<expression>.+?)\s+(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\s*$",
    re.IGNORECASE,
)
_SIMPLE_EXPRESSION_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$"
)


@dataclass(frozen=True)
class QueryParts:
    sql: str
    select: str
    from_clause: str
    where: str
    group_by: str
    having: str
    order_by: str
    limit: str
    distinct: bool


@dataclass(frozen=True)
class SourceRef:
    source: str
    alias: str
    columns: tuple[str, ...]
    scan_node: dict[str, Any]


def _validate_statement(sql: str, *, view: str | None = None) -> sqlparse.sql.Statement:
    if not isinstance(sql, str) or not sql.strip():
        raise BusinessError(E_SQL_SYNTAX, "SQL 文本为空", view=view, node_id="sql")
    try:
        statements = [stmt for stmt in sqlparse.parse(sql) if str(stmt).strip()]
    except Exception as exc:
        raise BusinessError(E_SQL_SYNTAX, "sqlparse 无法解析 SQL", view=view, node_id="sql", error=str(exc)) from exc
    if len(statements) != 1:
        raise BusinessError(
            E_SQL_SYNTAX,
            "只允许单条 SQL 语句",
            view=view,
            node_id="sql",
            statement_count=len(statements),
        )
    statement = statements[0]
    normalized = normalize_sql(sql).rstrip(";").strip()
    first_word = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)", normalized)
    if not first_word or first_word.group(1).upper() != "SELECT":
        raise BusinessError(
            E_SQL_UNSUPPORTED,
            "只支持 SELECT 查询",
            view=view,
            node_id="sql",
            statement_type=statement.get_type(),
        )
    return statement


def _clause_starts(sql: str) -> list[tuple[str, int, int]]:
    words = top_level_words(sql)
    starts: list[tuple[str, int, int]] = []
    index = 0
    seen_from = False
    while index < len(words):
        word, start, end = words[index]
        clause = ""
        consumed = 1
        if word == "FROM":
            clause = "from"
            seen_from = True
        elif word == "WHERE" and seen_from:
            clause = "where"
        elif word == "GROUP" and index + 1 < len(words) and words[index + 1][0] == "BY":
            clause = "group"
            consumed = 2
        elif word == "HAVING" and seen_from:
            clause = "having"
        elif word == "ORDER" and index + 1 < len(words) and words[index + 1][0] == "BY":
            clause = "order"
            consumed = 2
        elif word == "LIMIT" and seen_from:
            clause = "limit"
        elif word in {"UNION", "INTERSECT", "EXCEPT"}:
            raise BusinessError(
                E_SQL_UNSUPPORTED,
                "不支持集合运算",
                node_id="sql",
                keyword=word,
            )
        elif word == "SELECT" and index != 0:
            raise BusinessError(
                E_SQL_UNSUPPORTED,
                "不支持顶层子查询或第二个 SELECT",
                node_id="sql",
            )
        if clause:
            starts.append((clause, start, words[index + consumed - 1][2]))
            index += consumed
            continue
        index += 1
    return starts


def parse_query_parts(sql: str, *, view: str | None = None) -> QueryParts:
    _validate_statement(sql, view=view)
    normalized = normalize_sql(sql).rstrip(";").strip()
    try:
        starts = _clause_starts(normalized)
    except BusinessError as exc:
        if exc.view is None:
            exc.view = view
        raise
    from_starts = [item for item in starts if item[0] == "from"]
    if len(from_starts) > 1:
        raise BusinessError(E_SQL_UNSUPPORTED, "不支持多个 FROM 子句", view=view, node_id="sql")

    select_end = from_starts[0][1] if from_starts else len(normalized)
    select_text = normalized[normalized.upper().find("SELECT") + len("SELECT") : select_end].strip()
    distinct = False
    if re.match(r"^DISTINCT\b", select_text, re.IGNORECASE):
        distinct = True
        select_text = re.sub(r"^DISTINCT\b", "", select_text, count=1, flags=re.IGNORECASE).strip()
    if not select_text:
        raise BusinessError(E_SQL_SYNTAX, "SELECT 列表为空", view=view, node_id="sql")

    values = {
        "from": "",
        "where": "",
        "group": "",
        "having": "",
        "order": "",
        "limit": "",
    }
    for position, (name, _start, end) in enumerate(starts):
        next_start = starts[position + 1][1] if position + 1 < len(starts) else len(normalized)
        values[name] = normalized[end:next_start].strip()
    return QueryParts(
        sql=normalized,
        select=select_text,
        from_clause=values["from"],
        where=values["where"],
        group_by=values["group"],
        having=values["having"],
        order_by=values["order"],
        limit=values["limit"],
        distinct=distinct,
    )


def _top_level_regex_spans(text: str, pattern: re.Pattern[str]) -> list[re.Match[str]]:
    masked = mask_sql_literals(text)
    depth = 0
    matches: list[re.Match[str]] = []
    index = 0
    while index < len(masked):
        char = masked[index]
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth -= 1
            index += 1
            continue
        if depth == 0:
            match = pattern.match(masked, index)
            if match:
                matches.append(match)
                index = match.end()
                continue
        index += 1
    return matches


def _find_top_level_on(text: str) -> tuple[str, str]:
    masked = mask_sql_literals(text)
    depth = 0
    index = 0
    while index < len(masked):
        char = masked[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0:
            match = re.match(r"\bON\b", masked[index:], re.IGNORECASE)
            if match:
                return text[:index].strip(), text[index + match.end() :].strip()
        index += 1
    return text.strip(), ""


def _parse_table_ref(text: str, *, view: str | None) -> tuple[str, str]:
    if re.search(r"\bUSING\b", text, re.IGNORECASE):
        raise BusinessError(E_SQL_UNSUPPORTED, "暂不支持 JOIN USING", view=view, node_id="join")
    match = _TABLE_PATTERN.match(text.strip())
    if not match:
        raise BusinessError(
            E_SQL_SYNTAX,
            "FROM/JOIN 表引用不合法",
            view=view,
            node_id="scan",
            expression=text,
        )
    table = match.group("table")
    alias = match.group("alias") or table.split(".")[-1]
    if not valid_identifier(alias):
        raise BusinessError(E_SQL_SYNTAX, "表别名不合法", view=view, node_id="scan", alias=alias)
    return table, alias


def _make_node(
    node_type: str,
    *,
    children: list[dict[str, Any]],
    output_columns: list[str],
    predicate_columns: list[str],
    join_keys: list[dict[str, Any]],
    references: list[dict[str, str]],
    details: dict[str, Any],
) -> dict[str, Any]:
    summary_payload = {
        "type": node_type,
        "children": [child["summary"] for child in children],
        "output_columns": output_columns,
        "predicate_columns": sorted(set(predicate_columns)),
        "join_keys": join_keys,
        "references": references,
        "details": details,
    }
    summary = sha256_json(summary_payload)
    return {
        "node_id": f"nod_{summary}",
        "summary": summary,
        "type": node_type,
        "children": children,
        "output_columns": output_columns,
        "predicate_columns": sorted(set(predicate_columns)),
        "join_keys": join_keys,
        "references": references,
        "details": details,
    }


def _ref_key(reference: dict[str, str]) -> str:
    return f"{reference['source']}.{reference['column']}"


def _dedupe_refs(references: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for reference in references:
        key = (reference["source"], reference["column"])
        if key not in seen:
            seen.add(key)
            result.append({"source": reference["source"], "column": reference["column"]})
    return result


def _resolve_expression(
    expression: str,
    *,
    view: str,
    node_id: str,
    aliases: dict[str, str],
    source_schemas: dict[str, list[str]],
    derived_outputs: dict[str, list[dict[str, str]]] | None = None,
) -> tuple[list[dict[str, str]], list[str]]:
    masked = mask_sql_literals(expression)
    references: list[dict[str, str]] = []
    alias_references: list[str] = []

    def add_reference(qualifier_text: str, column: str) -> None:
        source = aliases.get(qualifier_text)
        if source is None and qualifier_text in source_schemas:
            source = qualifier_text
        if source is None:
            raise BusinessError(
                E_DEPENDENCY_MISSING,
                f"未知表或视图限定名: {qualifier_text}",
                view=view,
                node_id=node_id,
                qualifier=qualifier_text,
                column=column,
            )
        columns = source_schemas.get(source, [])
        if column == "*":
            references.extend({"source": source, "column": name} for name in columns)
            return
        if column not in columns:
            raise BusinessError(
                E_COLUMN_UNKNOWN,
                f"列不存在: {source}.{column}",
                view=view,
                node_id=node_id,
                source=source,
                column=column,
                expression=expression,
            )
        references.append({"source": source, "column": column})

    working = list(masked)
    for match in _QUALIFIED_PATTERN.finditer(masked):
        add_reference(match.group("qualifier"), match.group("column"))
        for pos in range(match.start(), match.end()):
            working[pos] = " "

    working_text = "".join(working)
    for match in _WORD_PATTERN.finditer(working_text):
        word = match.group("word")
        upper = word.upper()
        after = working_text[match.end() :]
        if re.match(r"\s*\(", after):
            continue
        if upper in _SQL_KEYWORDS or upper in _TYPE_WORDS:
            continue
        if match.start() > 0 and working_text[match.start() - 1] == ".":
            continue
        if match.end() < len(working_text) and working_text[match.end()] == ".":
            continue
        if derived_outputs and word in derived_outputs:
            references.extend(derived_outputs[word])
            alias_references.append(word)
            continue
        candidates = [
            source
            for alias, source in aliases.items()
            if word in source_schemas.get(source, [])
        ]
        candidate_sources = dedupe_preserving_order(source for source in candidates)
        if len(candidate_sources) == 1:
            references.append({"source": candidate_sources[0], "column": word})
        elif len(candidate_sources) > 1:
            raise BusinessError(
                E_SQL_AMBIGUOUS_COLUMN,
                f"未限定列名存在歧义: {word}",
                view=view,
                node_id=node_id,
                column=word,
                candidates=sorted(candidate_sources),
                expression=expression,
            )
        else:
            raise BusinessError(
                E_COLUMN_UNKNOWN,
                f"列不存在或无法解析: {word}",
                view=view,
                node_id=node_id,
                column=word,
                expression=expression,
            )
    return _dedupe_refs(references), alias_references


def _parse_join_keys(
    expression: str,
    *,
    view: str,
    aliases: dict[str, str],
    source_schemas: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    keys: list[dict[str, Any]] = []
    all_refs: list[dict[str, str]] = []
    for conjunction in split_by_top_level_keyword(expression, "AND"):
        refs, _aliases = _resolve_expression(
            conjunction,
            view=view,
            node_id="join",
            aliases=aliases,
            source_schemas=source_schemas,
        )
        all_refs.extend(refs)
        masked = mask_sql_literals(conjunction)
        equal_positions: list[int] = []
        depth = 0
        for index, char in enumerate(masked):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif depth == 0 and char == "=":
                before = masked[index - 1] if index > 0 else ""
                after = masked[index + 1] if index + 1 < len(masked) else ""
                if before not in "<>!=" and after != "=":
                    equal_positions.append(index)
        if len(equal_positions) == 1:
            split_at = equal_positions[0]
            left_refs, _ = _resolve_expression(
                conjunction[:split_at],
                view=view,
                node_id="join",
                aliases=aliases,
                source_schemas=source_schemas,
            )
            right_refs, _ = _resolve_expression(
                conjunction[split_at + 1 :],
                view=view,
                node_id="join",
                aliases=aliases,
                source_schemas=source_schemas,
            )
            if len(left_refs) == 1 and len(right_refs) == 1:
                keys.append(
                    {
                        "left": left_refs[0],
                        "right": right_refs[0],
                    }
                )
    keys.sort(key=lambda item: (_ref_key(item["left"]), _ref_key(item["right"])))
    return keys, _dedupe_refs(all_refs)


def _parse_relation(
    from_clause: str,
    *,
    view: str,
    source_schemas: dict[str, list[str]],
) -> tuple[dict[str, Any] | None, dict[str, str], list[str]]:
    if not from_clause:
        return None, {}, []
    join_matches = _top_level_regex_spans(from_clause, _JOIN_PATTERN)
    base_end = join_matches[0].start() if join_matches else len(from_clause)
    base_text = from_clause[:base_end].strip()
    base_parts = split_top_level(base_text) if base_text else []

    source_specs: list[tuple[str, str, str | None, str]] = []
    for part in base_parts:
        table, alias = _parse_table_ref(part, view=view)
        source_specs.append((table, alias, None, "CROSS"))

    for position, match in enumerate(join_matches):
        segment_end = join_matches[position + 1].start() if position + 1 < len(join_matches) else len(from_clause)
        segment = from_clause[match.end() : segment_end].strip()
        table_part, on_clause = _find_top_level_on(segment)
        table, alias = _parse_table_ref(table_part, view=view)
        kind = " ".join(match.group("kind").upper().split())
        source_specs.append((table, alias, on_clause or None, kind))

    aliases: dict[str, str] = {}
    scans: list[SourceRef] = []
    for source, alias, _on_clause, _kind in source_specs:
        if source not in source_schemas:
            raise BusinessError(
                E_DEPENDENCY_MISSING,
                f"依赖源未声明: {source}",
                view=view,
                node_id="scan",
                source=source,
            )
        if alias in aliases:
            raise BusinessError(
                E_SQL_SYNTAX,
                f"表别名重复: {alias}",
                view=view,
                node_id="scan",
                alias=alias,
            )
        aliases[alias] = source
        columns = list(source_schemas[source])
        references = [{"source": source, "column": column} for column in columns]
        scan = _make_node(
            "scan",
            children=[],
            output_columns=[f"{source}.{column}" for column in columns],
            predicate_columns=[],
            join_keys=[],
            references=references,
            details={"source": source, "alias": alias},
        )
        scans.append(
            SourceRef(
                source=source,
                alias=alias,
                columns=tuple(columns),
                scan_node=scan,
            )
        )

    if not scans:
        raise BusinessError(E_SQL_SYNTAX, "FROM 子句为空", view=view, node_id="sql")

    relation = scans[0].scan_node
    join_position = 0
    for source_spec, scan in zip(source_specs[1:], scans[1:]):
        _source, _alias, on_clause, kind = source_spec
        if kind == "CROSS":
            join_keys: list[dict[str, Any]] = []
            predicate_refs: list[dict[str, str]] = []
        else:
            if on_clause is None and "NATURAL" not in kind:
                raise BusinessError(
                    E_SQL_SYNTAX,
                    "JOIN 缺少 ON 条件",
                    view=view,
                    node_id="join",
                    join_kind=kind,
                )
            if on_clause:
                join_keys, predicate_refs = _parse_join_keys(
                    on_clause,
                    view=view,
                    aliases=aliases,
                    source_schemas=source_schemas,
                )
            else:
                join_keys, predicate_refs = [], []
        relation = _make_node(
            "join",
            children=[relation, scan.scan_node],
            output_columns=dedupe_preserving_order(
                relation["output_columns"] + scan.scan_node["output_columns"]
            ),
            predicate_columns=[_ref_key(reference) for reference in predicate_refs],
            join_keys=join_keys,
            references=predicate_refs,
            details={"join_kind": kind, "on": on_clause},
        )
        join_position += 1
    return relation, aliases, [scan.source for scan in scans]


def _extract_alias(expression: str) -> tuple[str, str | None]:
    as_match = _ALIAS_AS_PATTERN.match(expression.strip())
    if as_match:
        return as_match.group("expression").strip(), as_match.group("alias")
    implicit = _ALIAS_IMPLICIT_PATTERN.match(expression.strip())
    if implicit:
        prefix = implicit.group("expression").strip()
        if _SIMPLE_EXPRESSION_PATTERN.match(prefix) or prefix.endswith(")"):
            candidate = implicit.group("alias")
            if candidate.upper() not in _SQL_KEYWORDS:
                return prefix, candidate
    return expression.strip(), None


def _derive_output_name(expression: str) -> str:
    text = expression.strip()
    simple = _SIMPLE_EXPRESSION_PATTERN.match(text)
    if simple:
        return text.split(".")[-1]
    aggregate = re.match(
        r"^(?P<function>AVG|COUNT|MAX|MIN|SUM)\s*\(\s*(?P<inner>.*?)\s*\)\s*$",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if aggregate:
        function = aggregate.group("function").lower()
        inner = aggregate.group("inner").strip()
        inner = re.sub(r"^DISTINCT\s+", "", inner, flags=re.IGNORECASE)
        identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", inner)
        identifiers = [
            name for name in identifiers if name.upper() not in _SQL_KEYWORDS
        ]
        if identifiers:
            return f"{function}_{identifiers[-1].lower()}"
        return function
    return f"expr_{sha256_json(text)[:8]}"


def _parse_select_items(
    select_text: str,
    *,
    view: str,
    aliases: dict[str, str],
    source_schemas: dict[str, list[str]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    output_names: set[str] = set()
    for raw_item in split_top_level(select_text):
        expression, explicit_alias = _extract_alias(raw_item)
        if not expression:
            raise BusinessError(E_SQL_SYNTAX, "SELECT 项为空", view=view, node_id="project")

        wildcard_match = re.match(
            r"^(?:(?P<qualifier>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*)?\*$",
            expression.strip(),
        )
        if wildcard_match:
            qualifier = wildcard_match.group("qualifier")
            if explicit_alias:
                raise BusinessError(
                    E_SQL_UNSUPPORTED,
                    "通配投影不支持显式别名",
                    view=view,
                    node_id="project",
                )
            if qualifier:
                if qualifier not in aliases:
                    raise BusinessError(
                        E_DEPENDENCY_MISSING,
                        f"未知投影限定名: {qualifier}",
                        view=view,
                        node_id="project",
                    )
                source_names = [aliases[qualifier]]
            else:
                source_names = dedupe_preserving_order(aliases.values())
            for source in source_names:
                for column in source_schemas.get(source, []):
                    output_name = column
                    if output_name in output_names:
                        raise BusinessError(
                            E_SQL_DUPLICATE_COLUMN,
                            f"投影列名重复: {output_name}",
                            view=view,
                            node_id="project",
                            column=output_name,
                        )
                    output_names.add(output_name)
                    items.append(
                        {
                            "expression": f"{source}.{column}",
                            "output": output_name,
                            "aggregate": False,
                            "references": [{"source": source, "column": column}],
                            "alias_references": [],
                        }
                    )
            continue

        output_name = explicit_alias or _derive_output_name(expression)
        if not valid_identifier(output_name):
            raise BusinessError(
                E_SQL_SYNTAX,
                f"投影列名不合法: {output_name}",
                view=view,
                node_id="project",
            )
        if output_name in output_names:
            raise BusinessError(
                E_SQL_DUPLICATE_COLUMN,
                f"投影列名重复: {output_name}",
                view=view,
                node_id="project",
                column=output_name,
            )
        output_names.add(output_name)
        references, alias_refs = _resolve_expression(
            expression,
            view=view,
            node_id="project",
            aliases=aliases,
            source_schemas=source_schemas,
        )
        aggregate_match = re.search(
            r"\b(AVG|COUNT|MAX|MIN|SUM)\s*\(",
            mask_sql_literals(expression),
            re.IGNORECASE,
        )
        items.append(
            {
                "expression": expression,
                "output": output_name,
                "aggregate": bool(aggregate_match),
                "references": references,
                "alias_references": alias_refs,
            }
        )
    if not items:
        raise BusinessError(E_SQL_SYNTAX, "SELECT 列表为空", view=view, node_id="project")
    return items


def _flatten_nodes(node: dict[str, Any], path: str = "root") -> list[dict[str, Any]]:
    current = {
        "node_id": node["node_id"],
        "type": node["type"],
        "summary": node["summary"],
        "path": path,
        "output_columns": node["output_columns"],
        "predicate_columns": node["predicate_columns"],
        "join_keys": node["join_keys"],
        "references": node["references"],
    }
    result = [current]
    for index, child in enumerate(node["children"]):
        result.extend(_flatten_nodes(child, f"{path}.{index}"))
    return result


def parse_sql_plan(
    view: str,
    sql: str,
    source_schemas: dict[str, list[str]],
    *,
    dependency_names: set[str] | None = None,
) -> dict[str, Any]:
    """Parse one SELECT into a stable logical-plan document."""

    parts = parse_query_parts(sql, view=view)
    try:
        relation, aliases, sources = _parse_relation(
            parts.from_clause,
            view=view,
            source_schemas=source_schemas,
        )
        select_items = _parse_select_items(
            parts.select,
            view=view,
            aliases=aliases,
            source_schemas=source_schemas,
        )

        where_refs: list[dict[str, str]] = []
        if parts.where:
            where_refs, _ = _resolve_expression(
                parts.where,
                view=view,
                node_id="filter",
                aliases=aliases,
                source_schemas=source_schemas,
            )
            relation = _make_node(
                "filter",
                children=[relation] if relation else [],
                output_columns=relation["output_columns"] if relation else [],
                predicate_columns=[_ref_key(reference) for reference in where_refs],
                join_keys=[],
                references=where_refs,
                details={"expression": parts.where, "phase": "where"},
            )

        group_refs: list[dict[str, str]] = []
        if parts.group_by:
            for group_expression in split_top_level(parts.group_by):
                refs, _ = _resolve_expression(
                    group_expression,
                    view=view,
                    node_id="aggregate",
                    aliases=aliases,
                    source_schemas=source_schemas,
                )
                group_refs.extend(refs)
            group_refs = _dedupe_refs(group_refs)
            aggregate_outputs = [
                item["output"] for item in select_items if item["aggregate"]
            ]
            aggregate_refs = _dedupe_refs(
                group_refs
                + [
                    reference
                    for item in select_items
                    if item["aggregate"]
                    for reference in item["references"]
                ]
            )
            relation = _make_node(
                "aggregate",
                children=[relation] if relation else [],
                output_columns=dedupe_preserving_order(
                    [_ref_key(reference) for reference in group_refs] + aggregate_outputs
                ),
                predicate_columns=[_ref_key(reference) for reference in group_refs],
                join_keys=[],
                references=aggregate_refs,
                details={
                    "group_by": [_ref_key(reference) for reference in group_refs],
                    "aggregates": aggregate_outputs,
                },
            )

        derived_outputs = {
            item["output"]: item["references"]
            for item in select_items
        }
        if parts.having:
            having_refs, _ = _resolve_expression(
                parts.having,
                view=view,
                node_id="filter",
                aliases=aliases,
                source_schemas=source_schemas,
                derived_outputs=derived_outputs,
            )
            relation = _make_node(
                "filter",
                children=[relation] if relation else [],
                output_columns=relation["output_columns"] if relation else [],
                predicate_columns=[_ref_key(reference) for reference in having_refs],
                join_keys=[],
                references=having_refs,
                details={"expression": parts.having, "phase": "having"},
            )

        project_refs = _dedupe_refs(
            [reference for item in select_items for reference in item["references"]]
        )
        output_columns = [item["output"] for item in select_items]
        project = _make_node(
            "project",
            children=[relation] if relation else [],
            output_columns=output_columns,
            predicate_columns=[],
            join_keys=[],
            references=project_refs,
            details={
                "distinct": parts.distinct,
                "select_items": select_items,
                "order_by": parts.order_by or None,
                "limit": parts.limit or None,
            },
        )
    except BusinessError as exc:
        if exc.view is None:
            exc.view = view
        raise

    column_dependencies = {
        item["output"]: item["references"] for item in select_items
    }
    dependency_set = dependency_names if dependency_names is not None else set(source_schemas)
    dependencies = sorted({source for source in sources if source in dependency_set})
    plan_body = {
        "format": "gsb-logical-plan-v1",
        "view": view,
        "sql": parts.sql,
        "root": project,
        "output_columns": output_columns,
        "column_dependencies": column_dependencies,
        "sources": sources,
        "dependencies": dependencies,
    }
    plan_summary = sha256_json(
        {
            "format": plan_body["format"],
            "view": view,
            "root": project["summary"],
            "output_columns": output_columns,
            "column_dependencies": column_dependencies,
            "dependencies": dependencies,
        }
    )
    plan = dict(plan_body)
    plan["plan_summary"] = plan_summary
    plan["node_index"] = _flatten_nodes(project)
    return plan


def extract_sql_sources(sql: str, *, view: str | None = None) -> list[str]:
    """Infer FROM/JOIN source names without requiring their schemas."""

    parts = parse_query_parts(sql, view=view)
    if not parts.from_clause:
        return []
    join_matches = _top_level_regex_spans(parts.from_clause, _JOIN_PATTERN)
    base_end = join_matches[0].start() if join_matches else len(parts.from_clause)
    segments = [parts.from_clause[:base_end]]
    for position, match in enumerate(join_matches):
        segment_end = (
            join_matches[position + 1].start()
            if position + 1 < len(join_matches)
            else len(parts.from_clause)
        )
        segments.append(parts.from_clause[match.end() : segment_end])
    sources: list[str] = []
    for segment_index, segment in enumerate(segments):
        if segment_index == 0:
            table_parts = split_top_level(segment) if segment.strip() else []
        else:
            table_part, _on = _find_top_level_on(segment)
            table_parts = [table_part]
        for table_part in table_parts:
            table, _alias = _parse_table_ref(table_part, view=view)
            sources.append(table)
    return dedupe_preserving_order(sources)


def find_dependency_nodes(
    plan: dict[str, Any],
    source: str,
    column: str,
) -> list[str]:
    node_ids: list[str] = []
    for node in plan.get("node_index", []):
        if any(
            reference.get("source") == source and reference.get("column") == column
            for reference in node.get("references", [])
        ):
            node_ids.append(node["node_id"])
    return sorted(node_ids)


def find_dependency_nodes_for_columns(
    plan: dict[str, Any],
    source: str,
    columns: set[str],
) -> list[str]:
    node_ids: set[str] = set()
    for column in columns:
        node_ids.update(find_dependency_nodes(plan, source, column))
    return sorted(node_ids)


def verify_plan_integrity(plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if plan.get("format") != "gsb-logical-plan-v1":
        errors.append("plan.format")
    for node in plan.get("node_index", []):
        # The node index intentionally omits child summaries and free-form
        # details, so full summary verification is performed in the store.
        if not node.get("node_id", "").startswith("nod_"):
            errors.append(f"node_id:{node.get('path', '?')}")
    return errors

