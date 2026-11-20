from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .errors import error


KEYWORDS = {
    "SELECT",
    "FROM",
    "WHERE",
    "JOIN",
    "LEFT",
    "RIGHT",
    "FULL",
    "INNER",
    "OUTER",
    "CROSS",
    "ON",
    "AS",
    "GROUP",
    "BY",
    "ORDER",
    "LIMIT",
    "OFFSET",
    "HAVING",
    "UNION",
    "ALL",
    "DISTINCT",
    "AND",
    "OR",
    "NOT",
    "NULL",
    "IS",
    "IN",
    "EXISTS",
    "BETWEEN",
    "LIKE",
    "GLOB",
    "CASE",
    "WHEN",
    "THEN",
    "ELSE",
    "END",
    "CAST",
    "COUNT",
    "SUM",
    "AVG",
    "MIN",
    "MAX",
    "ROUND",
    "COALESCE",
    "IFNULL",
    "NULLIF",
    "ASC",
    "DESC",
    "WITH",
    "RECURSIVE",
    "CREATE",
    "TRIGGER",
    "AFTER",
    "BEFORE",
    "INSTEAD",
    "OF",
    "UPDATE",
    "INSERT",
    "DELETE",
    "INTO",
    "VALUES",
    "BEGIN",
    "FOR",
    "EACH",
    "ROW",
    "SET",
    "USING",
    "PRIMARY",
    "KEY",
    "INTEGER",
    "REAL",
    "TEXT",
    "BLOB",
    "NUMERIC",
    "BOOLEAN",
    "DATE",
    "DATETIME",
    "DECIMAL",
    "TRUE",
    "FALSE",
    "CURRENT_TIMESTAMP",
}


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    start: int
    end: int
    quoted: bool = False

    @property
    def upper(self) -> str:
        return self.value.upper()


def tokenize(sql: str) -> List[Token]:
    tokens: List[Token] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            i += 2
            while i < n and sql[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end < 0:
                raise error("E_SQL", "unterminated SQL block comment")
            i = end + 2
            continue
        if ch == "'":
            start = i
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            else:
                raise error("E_SQL", "unterminated SQL string literal")
            tokens.append(Token("string", sql[start:i], start, i))
            continue
        if ch in ('"', "`", "["):
            start = i
            close = "]" if ch == "[" else ch
            i += 1
            value_chars = []
            while i < n:
                if sql[i] == close:
                    if close != "]" and i + 1 < n and sql[i + 1] == close:
                        value_chars.append(close)
                        i += 2
                        continue
                    i += 1
                    break
                value_chars.append(sql[i])
                i += 1
            else:
                raise error("E_SQL", "unterminated quoted identifier")
            tokens.append(Token("ident", "".join(value_chars), start, i, quoted=True))
            continue
        if ch.isalpha() or ch == "_" or ord(ch) > 127:
            start = i
            i += 1
            while i < n and (sql[i].isalnum() or sql[i] in "_$" or ord(sql[i]) > 127):
                i += 1
            value = sql[start:i]
            kind = "keyword" if value.upper() in KEYWORDS else "ident"
            tokens.append(Token(kind, value, start, i))
            continue
        if ch.isdigit():
            start = i
            i += 1
            while i < n and (sql[i].isdigit() or sql[i] in ".eE+-"):
                if sql[i] in "+-" and sql[i - 1] not in "eE":
                    break
                i += 1
            tokens.append(Token("number", sql[start:i], start, i))
            continue
        if i + 1 < n and sql[i : i + 2] in ("<=", ">=", "<>", "!=", "||", "=="):
            tokens.append(Token("symbol", sql[i : i + 2], i, i + 2))
            i += 2
            continue
        tokens.append(Token("symbol", ch, i, i + 1))
        i += 1
    return tokens


def validate_single_select(sql: str, path: str) -> None:
    tokens = tokenize(sql)
    if not tokens:
        raise error("E_SQL", "SQL statement is empty", path=path)
    semicolons = [token for token in tokens if token.value == ";"]
    if semicolons:
        semicolon = semicolons[0]
        if len(semicolons) != 1 or any(t.start > semicolon.start for t in tokens if t.value != ";"):
            raise error("E_SQL", "only one SQL statement is allowed", path=path)
        tokens = [t for t in tokens if t.value != ";"]
    if not tokens or tokens[0].upper not in ("SELECT", "WITH"):
        raise error("E_SQL", "template SQL must start with SELECT or WITH", path=path)


class _SchemaLookup:
    def __init__(self, schema: Dict[str, Any]) -> None:
        self.tables = {table["name"]: table for table in schema.get("tables", [])}
        self.views = {view["name"]: view for view in schema.get("views", [])}
        self.columns = {
            table_name: {column["name"] for column in table.get("columns", [])}
            for table_name, table in self.tables.items()
        }

    def has_relation(self, name: str) -> bool:
        return name in self.tables or name in self.views


def parse_query_dependencies(sql: str, schema: Dict[str, Any], path: str = "sql") -> Dict[str, Any]:
    validate_single_select(sql, path)
    tokens = tokenize(sql)
    lookup = _SchemaLookup(schema)
    relation_refs: Set[str] = set()
    view_refs: Set[str] = set()
    aliases: Dict[str, Optional[str]] = {}
    relation_order: List[str] = []

    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.upper in ("FROM", "JOIN"):
            i += 1
            if i < len(tokens) and tokens[i].upper == "(":
                i += 1
                continue
            if i >= len(tokens):
                break
            if i + 2 < len(tokens) and tokens[i + 1].value == ".":
                # schema-qualified relation
                name_token = tokens[i + 2]
                i += 3
            else:
                name_token = tokens[i]
                i += 1
            relation = name_token.value
            if not lookup.has_relation(relation):
                raise error(
                    "E_SQL_REFERENCE",
                    "SQL references an unknown table or view",
                    path=path,
                    relation=relation,
                )
            relation_refs.add(relation)
            relation_order.append(relation)
            alias: Optional[str] = relation
            if i < len(tokens) and tokens[i].upper == "AS":
                i += 1
                if i < len(tokens):
                    alias = tokens[i].value
                    i += 1
            elif (
                i < len(tokens)
                and tokens[i].kind == "ident"
                and tokens[i].upper not in KEYWORDS
            ):
                alias = tokens[i].value
                i += 1
            aliases[relation] = relation
            if alias:
                aliases[alias] = relation
            continue
        i += 1

    columns: Set[str] = set()
    table_only_refs: Set[str] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind in ("ident", "keyword") and token.upper not in KEYWORDS:
            if index + 2 < len(tokens) and tokens[index + 1].value == ".":
                second = tokens[index + 2]
                qualifier = token.value
                relation = aliases.get(qualifier, qualifier if lookup.has_relation(qualifier) else None)
                if relation is None:
                    if second.value == "*":
                        index += 3
                        continue
                    raise error(
                        "E_SQL_REFERENCE",
                        "qualified column reference has an unknown qualifier",
                        path=path,
                        qualifier=qualifier,
                        column=second.value,
                    )
                if relation in lookup.views:
                    view_refs.add(relation)
                elif second.value != "*" and second.value not in lookup.columns.get(relation, set()):
                    raise error(
                        "E_SQL_REFERENCE",
                        "SQL references an unknown column",
                        path=path,
                        table=relation,
                        column=second.value,
                    )
                if second.value == "*":
                    for column in lookup.tables.get(relation, {}).get("columns", []):
                        columns.add(f"{relation}.{column['name']}")
                elif relation in lookup.tables:
                    columns.add(f"{relation}.{second.value}")
                index += 3
                continue
        index += 1

    # A star or a bare column is resolved only against the relations introduced
    # by this simple SELECT. This keeps the parser deterministic and rejects
    # ambiguous templates instead of guessing.
    visible_tables = [name for name in relation_order if name in lookup.tables]
    for index, token in enumerate(tokens):
        if token.value != "*":
            continue
        previous = tokens[index - 1] if index > 0 else None
        if previous is not None and previous.value not in ("(", ","):
            if previous.upper not in ("SELECT", "DISTINCT", "ALL"):
                continue
        if previous is not None and previous.value == "(":
            continue
        for table_name in visible_tables:
            for column in lookup.tables[table_name].get("columns", []):
                columns.add(f"{table_name}.{column['name']}")
    for table_name in visible_tables:
        for column_name in lookup.columns.get(table_name, set()):
            pattern = re.compile(rf"(?<![\w.]){re.escape(column_name)}(?!\w)")
            if pattern.search(sql) and f"{table_name}.{column_name}" not in columns:
                owners = [
                    name
                    for name in visible_tables
                    if column_name in lookup.columns.get(name, set())
                ]
                if len(owners) == 1:
                    columns.add(f"{owners[0]}.{column_name}")
                elif len(owners) > 1:
                    raise error(
                        "E_TEMPLATE_AMBIGUOUS",
                        "unqualified column reference is ambiguous",
                        path=path,
                        column=column_name,
                        tables=sorted(owners),
                    )

    # A template that only touches a view still needs the view node. Underlying
    # column edges are recovered from the view's own declared dependencies.
    for relation in relation_order:
        if relation in lookup.views:
            view_refs.add(relation)
        else:
            table_only_refs.add(relation)

    references = sorted(
        [f"column:{column}" for column in columns]
        + [f"table:{table}" for table in table_only_refs]
        + [f"view:{view}" for view in view_refs]
    )
    return {
        "relations": sorted(relation_order),
        "tables": sorted(table_only_refs),
        "views": sorted(view_refs),
        "columns": sorted(columns),
        "references": references,
    }


def rewrite_identifiers(
    sql: str,
    schema: Dict[str, Any],
    table_renames: Dict[str, str],
    column_renames: Dict[Tuple[str, str], Tuple[str, str]],
) -> str:
    tokens = tokenize(sql)
    lookup = _SchemaLookup(schema)
    replacements: List[Tuple[int, int, str]] = []
    aliases: Dict[str, str] = {}

    i = 0
    while i < len(tokens):
        if tokens[i].upper in ("FROM", "JOIN"):
            i += 1
            if i < len(tokens):
                relation_token = tokens[i]
                if i + 2 < len(tokens) and tokens[i + 1].value == ".":
                    relation_token = tokens[i + 2]
                    i += 1
                relation = relation_token.value
                if relation in table_renames:
                    replacements.append((relation_token.start, relation_token.end, table_renames[relation]))
                alias = relation
                i += 1
                if i < len(tokens) and tokens[i].upper == "AS":
                    i += 1
                    if i < len(tokens):
                        alias = tokens[i].value
                        i += 1
                elif i < len(tokens) and tokens[i].kind == "ident" and tokens[i].upper not in KEYWORDS:
                    alias = tokens[i].value
                    i += 1
                aliases[alias] = relation
                aliases[relation] = relation
                continue
        i += 1

    i = 0
    while i + 2 < len(tokens):
        first, dot, second = tokens[i], tokens[i + 1], tokens[i + 2]
        if first.kind in ("ident", "keyword") and dot.value == "." and second.kind in ("ident", "keyword"):
            relation = aliases.get(first.value, first.value if lookup.has_relation(first.value) else None)
            if relation and (relation, second.value) in column_renames:
                new_relation, new_column = column_renames[(relation, second.value)]
                replacements.append((first.start, first.end, new_relation))
                replacements.append((second.start, second.end, new_column))
            i += 3
            continue
        i += 1

    if not replacements:
        return sql.strip()
    out: List[str] = []
    cursor = 0
    for start, end, replacement in sorted(set(replacements)):
        out.append(sql[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(sql[cursor:])
    return "".join(out).strip()


def rewrite_cast_types(
    sql: str,
    schema: Dict[str, Any],
    column_type_changes: Dict[Tuple[str, str], str],
) -> str:
    if not column_type_changes:
        return sql
    tokens = tokenize(sql)
    lookup = _SchemaLookup(schema)
    aliases: Dict[str, str] = {}
    i = 0
    while i < len(tokens):
        if tokens[i].upper in ("FROM", "JOIN"):
            i += 1
            if i >= len(tokens):
                break
            relation_token = tokens[i]
            if i + 2 < len(tokens) and tokens[i + 1].value == ".":
                relation_token = tokens[i + 2]
                i += 1
            relation = relation_token.value
            aliases[relation] = relation
            i += 1
            if i < len(tokens) and tokens[i].upper == "AS":
                i += 1
                if i < len(tokens):
                    aliases[tokens[i].value] = relation
                    i += 1
            elif i < len(tokens) and tokens[i].kind == "ident" and tokens[i].upper not in KEYWORDS:
                aliases[tokens[i].value] = relation
                i += 1
            continue
        i += 1

    replacements: List[Tuple[int, int, str]] = []
    i = 0
    while i < len(tokens):
        if tokens[i].upper != "CAST" or i + 1 >= len(tokens) or tokens[i + 1].value != "(":
            i += 1
            continue
        open_index = i + 1
        depth = 0
        close_index = None
        as_index = None
        for cursor in range(open_index, len(tokens)):
            if tokens[cursor].value == "(":
                depth += 1
            elif tokens[cursor].value == ")":
                depth -= 1
                if depth == 0:
                    close_index = cursor
                    break
            elif tokens[cursor].upper == "AS" and depth == 1 and as_index is None:
                as_index = cursor
        if close_index is None or as_index is None:
            i += 1
            continue
        changed = None
        cursor = open_index + 1
        while cursor + 2 < as_index:
            qualifier, dot, column = tokens[cursor], tokens[cursor + 1], tokens[cursor + 2]
            if qualifier.kind in ("ident", "keyword") and dot.value == ".":
                table = aliases.get(qualifier.value, qualifier.value)
                if (table, column.value) in column_type_changes:
                    changed = column_type_changes[(table, column.value)]
                    break
                cursor += 3
                continue
            cursor += 1
        if changed is not None:
            replacements.append((tokens[as_index].end, tokens[close_index].start, f" {changed}"))
        i = close_index + 1
    if not replacements:
        return sql
    out: List[str] = []
    cursor = 0
    for start, end, replacement in sorted(replacements):
        out.append(sql[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(sql[cursor:])
    return "".join(out).strip()


def normalize_sql_text(sql: str) -> str:
    tokens = tokenize(sql)
    normalized: List[str] = []
    for token in tokens:
        normalized.append(token.value.upper() if token.kind == "keyword" else token.value)
    return " ".join(normalized)


def strip_create_view_prefix(sql: str, name: str) -> str:
    pattern = re.compile(
        rf"^\s*CREATE\s+(?:TEMP(?:ORARY)?\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        rf"(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_$]*)\s+AS\s+",
        re.IGNORECASE | re.DOTALL,
    )
    return pattern.sub("", sql, count=1).strip()

