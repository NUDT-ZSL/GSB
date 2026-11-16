"""Deterministic parser and normalizer for the supported join-query subset."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .catalog import Catalog, ColumnDef, TableDef
from .errors import QueryError, TypeMismatchError
from .typesys import (
    COMPARISON_OPS,
    NUMERIC_TYPES,
    literal_comparable,
    type_comparable,
)


@dataclass(frozen=True)
class Token:
    kind: str
    value: Any
    position: int


@dataclass(frozen=True)
class ColumnRef:
    table: str
    column: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.table, self.column)

    def render(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass(frozen=True)
class Literal:
    value: Any
    type: str


@dataclass(frozen=True)
class Predicate:
    left: ColumnRef
    op: str
    right: ColumnRef | Literal

    @property
    def tables(self) -> frozenset[str]:
        result = {self.left.table}
        if isinstance(self.right, ColumnRef):
            result.add(self.right.table)
        return frozenset(result)

    def render(self) -> str:
        if isinstance(self.right, ColumnRef):
            right = self.right.render()
        else:
            right = render_literal(self.right)
        return f"{self.left.render()} {self.op} {right}"


@dataclass(frozen=True)
class JoinEdge:
    kind: str
    right_table: str
    predicates: tuple[Predicate, ...]


@dataclass(frozen=True)
class Query:
    source_text: str
    canonical_sql: str
    query_id: str
    tables: tuple[str, ...]
    projection: tuple[ColumnRef, ...]
    local_filters: tuple[tuple[str, tuple[Predicate, ...]], ...]
    join_predicates: tuple[Predicate, ...]
    post_filters: tuple[Predicate, ...]
    edges: tuple[JoinEdge, ...]
    has_outer: bool
    table_defs: tuple[TableDef, ...]

    @property
    def table_keys(self) -> tuple[str, ...]:
        return tuple(table.key for table in self.table_defs)

    def filters_for(self, table: str) -> tuple[Predicate, ...]:
        for key, predicates in self.local_filters:
            if key == table:
                return predicates
        return ()

    def output_columns(self, catalog: Catalog) -> tuple[ColumnDef, ...]:
        result: list[ColumnDef] = []
        for ref in self.projection:
            table = catalog.table(ref.table)
            if table is None:
                raise QueryError(f"unknown table {ref.table!r} in projection", "query.projection")
            column = table.column(ref.column)
            if column is None:
                raise QueryError(f"unknown column {ref.column!r} in projection", "query.projection")
            result.append(column)
        return tuple(result)


KEYWORDS = frozenset(
    {
        "select",
        "from",
        "where",
        "join",
        "inner",
        "left",
        "right",
        "full",
        "cross",
        "outer",
        "on",
        "and",
        "or",
        "as",
    }
)
FORBIDDEN_KEYWORDS = frozenset(
    {
        "right",
        "full",
        "cross",
        "group",
        "having",
        "order",
        "limit",
        "offset",
        "union",
        "except",
        "intersect",
        "with",
        "distinct",
        "top",
        "window",
        "returning",
    }
)
AGGREGATES = frozenset({"count", "sum", "avg", "min", "max", "total"})
FLIP_OP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "=", "<>": "<>", "!=": "<>"}


def _strip_comments(sql: str) -> str:
    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    return re.sub(r"--[^\n]*|/\*.*?\*/", blank, sql, flags=re.DOTALL)


def tokenize(sql: str) -> list[Token]:
    text = _strip_comments(sql)
    tokens: list[Token] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char == "'":
            start = index
            index += 1
            parts: list[str] = []
            while index < length:
                if text[index] == "'":
                    if index + 1 < length and text[index + 1] == "'":
                        parts.append("'")
                        index += 2
                        continue
                    index += 1
                    break
                parts.append(text[index])
                index += 1
            else:
                raise QueryError("unterminated string literal", f"sql@{start}")
            tokens.append(Token("string", "".join(parts), start))
            continue
        if char == '"':
            start = index
            index += 1
            parts = []
            while index < length:
                if text[index] == '"':
                    if index + 1 < length and text[index + 1] == '"':
                        parts.append('"')
                        index += 2
                        continue
                    index += 1
                    break
                parts.append(text[index])
                index += 1
            else:
                raise QueryError("unterminated quoted identifier", f"sql@{start}")
            value = "".join(parts)
            if not value:
                raise QueryError("quoted identifier must not be empty", f"sql@{start}")
            tokens.append(Token("ident", value, start))
            continue
        number_match = re.match(r"(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?", text[index:])
        if number_match:
            raw = number_match.group(0)
            is_negative = False
            # A leading minus is part of a numeric literal only when the
            # previous emitted token cannot end an operand.
            if index > 0 and text[index - 1] == "-" and (
                not tokens or tokens[-1].kind in {"op", "comma", "lparen"}
            ):
                # This path is mostly unreachable because '-' is consumed
                # separately below; retained for clarity.
                is_negative = True
            value: int | float
            if "." in raw or "e" in raw.lower():
                value = float(raw)
            else:
                value = int(raw)
            if is_negative:
                value = -value
            tokens.append(Token("number", value, index))
            index += len(raw)
            continue
        ident_match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", text[index:])
        if ident_match:
            raw = ident_match.group(0)
            tokens.append(Token("ident", raw, index))
            index += len(raw)
            continue
        two = text[index : index + 2]
        if two in {"<=", ">=", "<>", "!="}:
            tokens.append(Token("op", "<>" if two == "!=" else two, index))
            index += 2
            continue
        if char in "=<>,.*();+-":
            if char == "-" and index + 1 < length and re.match(r"\d", text[index + 1]):
                number_match = re.match(r"\d+(?:\.\d*)?(?:[eE][+-]?\d+)?", text[index + 1 :])
                assert number_match is not None
                raw = number_match.group(0)
                value = float(raw) if "." in raw or "e" in raw.lower() else int(raw)
                tokens.append(Token("number", -value, index))
                index += 1 + len(raw)
                continue
            kind = {
                "=": "op",
                "<": "op",
                ">": "op",
                ",": "comma",
                ".": "dot",
                "*": "star",
                "(": "lparen",
                ")": "rparen",
                ";": "semi",
            }.get(char, "unsupported")
            if kind == "unsupported":
                raise QueryError(f"unsupported token {char!r}", f"sql@{index}")
            tokens.append(Token(kind, char, index))
            index += 1
            continue
        raise QueryError(f"unsupported character {char!r}", f"sql@{index}")

    lowered = [(index, token) for index, token in enumerate(tokens) if token.kind == "ident"]
    for index, token in lowered:
        word = str(token.value).lower()
        if word in FORBIDDEN_KEYWORDS:
            raise QueryError(
                f"unsupported SQL feature or join direction: {word.upper()}",
                f"sql@{token.position}",
                feature=word,
            )
        if word in AGGREGATES and index + 1 < len(tokens) and tokens[index + 1].kind == "lparen":
            raise QueryError("aggregate functions are not supported", f"sql@{token.position}")
    if any(token.kind in {"lparen", "rparen"} for token in tokens):
        pos = next(token.position for token in tokens if token.kind in {"lparen", "rparen"})
        raise QueryError(
            "subqueries and parenthesized expressions are not supported",
            f"sql@{pos}",
        )
    return tokens


def _literal_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "real"
    if isinstance(value, str):
        return "text"
    if value is None:
        return "null"
    return "unknown"


def render_literal(literal: Literal) -> str:
    if literal.type == "boolean":
        return "true" if literal.value else "false"
    if literal.type == "null":
        return "null"
    if literal.type in {"integer", "real"}:
        value = literal.value
        if isinstance(value, float):
            if value == 0.0:
                return "0"
            if value.is_integer():
                return str(int(value))
            return format(value, ".15g")
        return str(value)
    return "'" + str(literal.value).replace("'", "''") + "'"


@dataclass(frozen=True)
class _RawColumn:
    qualifier: str | None
    column: str
    position: int


@dataclass(frozen=True)
class _RawPredicate:
    left: _RawColumn | Literal
    op: str
    right: _RawColumn | Literal
    position: int


@dataclass(frozen=True)
class _RawJoin:
    kind: str
    table: str
    alias: str
    predicates: tuple[_RawPredicate, ...]


class QueryParser:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self._tokens: list[Token] = []
        self._index = 0
        self._aliases: dict[str, str] = {}
        self._alias_tables: dict[str, TableDef] = {}
        self._referenced_tables: list[str] = []
        self._alias_positions: dict[str, int] = {}
        self._order: list[str] = []

    def parse(self, sql: str) -> Query:
        if not isinstance(sql, str) or not sql.strip():
            raise QueryError("query must be a non-empty string", "sql")
        self._tokens = tokenize(sql)
        self._index = 0
        self._aliases = {}
        self._alias_tables = {}
        self._referenced_tables = []
        self._alias_positions = {}
        self._order = []

        self._expect_word("select")
        raw_projection = self._parse_projection()
        self._expect_word("from")
        first = self._parse_table_ref()

        raw_joins: list[_RawJoin] = []
        while not self._at_end() and self._peek_word() not in {"where"}:
            kind = self._parse_join_keyword()
            right_ref = self._parse_table_ref()
            self._expect_word("on")
            predicates = self._parse_condition(stop_words={"join", "inner", "left", "where"})
            raw_joins.append(_RawJoin(kind, right_ref[0], right_ref[1], predicates))

        if len(self._referenced_tables) > 3:
            raise QueryError("at most three tables are supported", "query.tables")

        raw_where: tuple[_RawPredicate, ...] = ()
        if self._peek_word() == "where":
            self._consume_word("where")
            raw_where = self._parse_condition(stop_words=set())
        if self._peek_kind() == "semi":
            self._index += 1
        if not self._at_end():
            token = self._tokens[self._index]
            raise QueryError("unexpected trailing SQL", f"sql@{token.position}")

        has_outer = any(join.kind == "left" for join in raw_joins)

        join_predicates: list[Predicate] = []
        edges: list[JoinEdge] = []
        available = {self._order[0]}
        for raw_join in raw_joins:
            context = set(available)
            context.add(raw_join.table)
            edge_predicates: list[Predicate] = []
            for raw_predicate in raw_join.predicates:
                predicate = self._normalize_predicate(raw_predicate, f"sql@{raw_predicate.position}")
                if not predicate.tables <= context:
                    extra = sorted(predicate.tables - context)
                    raise QueryError(
                        "ON condition references a table that is not available yet",
                        f"sql@{raw_predicate.position}",
                        unavailable_tables=extra,
                    )
                edge_predicates.append(predicate)
                join_predicates.append(predicate)
            edges.append(JoinEdge(raw_join.kind, raw_join.table, tuple(sorted(edge_predicates, key=_predicate_key))))
            available.add(raw_join.table)

        canonical_order = (
            list(self._order)
            if has_outer
            else self._canonical_inner_order(self._order, join_predicates)
        )
        projection = self._expand_projection(raw_projection, canonical_order)

        local_filter_lists: dict[str, list[Predicate]] = {key: [] for key in canonical_order}
        post_filters: list[Predicate] = []
        for raw_predicate in raw_where:
            predicate = self._normalize_predicate(raw_predicate, f"sql@{raw_predicate.position}")
            # With LEFT JOINs, WHERE predicates are evaluated after the outer
            # join.  Pushing a right-side predicate into the scan would retain
            # unmatched NULL-padded rows and change SQL semantics.  A uniform
            # post-join placement is deliberately conservative and correct.
            if len(predicate.tables) == 1 and not has_outer:
                local_filter_lists[next(iter(predicate.tables))].append(predicate)
            else:
                post_filters.append(predicate)

        canonical_sql = self._build_canonical_sql(
            projection,
            canonical_order,
            has_outer,
            tuple(edges),
            join_predicates,
            local_filter_lists,
            post_filters,
        )
        query_id = "q_" + hashlib.sha256(canonical_sql.encode("utf-8")).hexdigest()[:24]
        local_filters = tuple(
            (key, tuple(sorted(local_filter_lists[key], key=_predicate_key)))
            for key in canonical_order
        )
        table_defs = tuple(self.catalog.table(key) for key in canonical_order)
        if any(table is None for table in table_defs):
            raise QueryError("unknown table in parsed query", "query.tables")
        return Query(
            source_text=sql,
            canonical_sql=canonical_sql,
            query_id=query_id,
            tables=tuple(canonical_order),
            projection=projection,
            local_filters=local_filters,
            join_predicates=tuple(sorted(join_predicates, key=_predicate_key)),
            post_filters=tuple(sorted(post_filters, key=_predicate_key)),
            edges=tuple(edges),
            has_outer=has_outer,
            table_defs=table_defs,  # type: ignore[arg-type]
        )

    # ----- token helpers -------------------------------------------------

    def _at_end(self) -> bool:
        return self._index >= len(self._tokens)

    def _peek(self) -> Token | None:
        if self._at_end():
            return None
        return self._tokens[self._index]

    def _peek_kind(self) -> str | None:
        token = self._peek()
        return token.kind if token else None

    def _peek_word(self) -> str | None:
        token = self._peek()
        if token is not None and token.kind == "ident":
            return str(token.value).lower()
        return None

    def _consume(self) -> Token:
        token = self._peek()
        if token is None:
            raise QueryError("unexpected end of query", "sql@%d" % len(self._tokens))
        self._index += 1
        return token

    def _expect_kind(self, kind: str) -> Token:
        token = self._peek()
        if token is None or token.kind != kind:
            position = token.position if token else 0
            raise QueryError(f"expected {kind}", f"sql@{position}")
        return self._consume()

    def _expect_word(self, word: str) -> Token:
        token = self._peek()
        if token is None or token.kind != "ident" or str(token.value).lower() != word:
            position = token.position if token else 0
            raise QueryError(f"expected {word.upper()}", f"sql@{position}")
        return self._consume()

    def _consume_word(self, word: str) -> None:
        self._expect_word(word)

    def _parse_join_keyword(self) -> str:
        word = self._peek_word()
        if word == "join":
            self._consume()
            return "inner"
        if word == "inner":
            self._consume()
            self._expect_word("join")
            return "inner"
        if word == "left":
            self._consume()
            if self._peek_word() == "outer":
                self._consume()
            self._expect_word("join")
            return "left"
        token = self._peek()
        position = token.position if token else 0
        raise QueryError("expected JOIN, INNER JOIN, or LEFT JOIN", f"sql@{position}")

    # ----- parsing -------------------------------------------------------

    def _parse_projection(self) -> list[_RawColumn | tuple[str, str] | None]:
        result: list[_RawColumn | tuple[str, str] | None] = []
        while True:
            token = self._peek()
            if token is None:
                raise QueryError("expected select list", "sql")
            if token.kind == "star":
                self._consume()
                result.append(None)
            else:
                first = self._expect_kind("ident")
                if self._peek_kind() == "dot":
                    self._consume()
                    if self._peek_kind() == "star":
                        self._consume()
                        result.append((str(first.value).lower(), "*"))
                    else:
                        second = self._expect_kind("ident")
                        result.append(_RawColumn(str(first.value), str(second.value), first.position))
                else:
                    result.append(_RawColumn(None, str(first.value), first.position))
            # Output aliases affect only the caller's column label, not the
            # relational value.  Consuming and dropping them keeps equivalent
            # alias spellings on the same canonical query.
            if self._peek_word() == "as":
                self._consume()
                self._expect_kind("ident")
            if self._peek_kind() == "comma":
                self._consume()
                continue
            break
        if not result:
            raise QueryError("select list must not be empty", "query.projection")
        return result

    def _parse_table_ref(self) -> tuple[str, str]:
        name_token = self._expect_kind("ident")
        table_name = str(name_token.value)
        table = self.catalog.table(table_name)
        if table is None:
            raise QueryError(
                f"unknown table {table_name!r}",
                f"sql@{name_token.position}",
                reason="unknown_ref",
                ref_type="table",
            )
        self._referenced_tables.append(table.key)
        if self._referenced_tables.count(table.key) > 1:
            raise QueryError(f"table {table_name!r} is joined more than once", f"sql@{name_token.position}")

        alias = table.key
        if self._peek_word() == "as":
            self._consume()
            alias_token = self._expect_kind("ident")
            alias = str(alias_token.value).lower()
            alias_position = alias_token.position
        elif (
            self._peek() is not None
            and self._peek().kind == "ident"
            and self._peek_word() not in {"join", "inner", "left", "where", "on"}
        ):
            alias_token = self._consume()
            alias = str(alias_token.value).lower()
            alias_position = alias_token.position
        else:
            alias_position = name_token.position

        if alias in self._aliases:
            raise QueryError(f"duplicate table alias {alias!r}", f"sql@{alias_position}")
        self._aliases[alias] = table.key
        self._alias_tables[alias] = table
        self._alias_positions[alias] = alias_position
        self._order.append(table.key)
        return table.key, alias

    def _parse_condition(self, stop_words: set[str]) -> tuple[_RawPredicate, ...]:
        predicates = [self._parse_predicate()]
        while self._peek_word() == "and":
            self._consume()
            predicates.append(self._parse_predicate())
        if self._peek_word() == "or":
            token = self._peek()
            raise QueryError("OR is not supported; use AND", f"sql@{token.position if token else 0}")
        if not self._at_end() and self._peek_kind() not in {"semi"}:
            word = self._peek_word()
            if word not in stop_words and word not in {"where"}:
                token = self._peek()
                raise QueryError("unsupported condition syntax", f"sql@{token.position if token else 0}")
        return tuple(predicates)

    def _parse_predicate(self) -> _RawPredicate:
        left = self._parse_operand()
        op_token = self._expect_kind("op")
        op = str(op_token.value)
        if op == "!=":
            op = "<>"
        right = self._parse_operand()
        return _RawPredicate(left, op, right, op_token.position)

    def _parse_operand(self) -> _RawColumn | Literal:
        token = self._peek()
        if token is None:
            raise QueryError("expected predicate operand", "sql")
        if token.kind == "string":
            self._consume()
            return Literal(token.value, "text")
        if token.kind == "number":
            self._consume()
            return Literal(token.value, _literal_type(token.value))
        if token.kind == "ident":
            word = str(token.value).lower()
            if word in {"true", "false"}:
                self._consume()
                return Literal(word == "true", "boolean")
            if word == "null":
                self._consume()
                return Literal(None, "null")
            first = self._consume()
            if self._peek_kind() == "dot":
                self._consume()
                second = self._expect_kind("ident")
                return _RawColumn(str(first.value), str(second.value), first.position)
            return _RawColumn(None, str(first.value), first.position)
        raise QueryError("expected column reference or literal", f"sql@{token.position}")

    # ----- resolution/normalization --------------------------------------

    def _resolve_column(self, raw: _RawColumn) -> ColumnRef:
        table: TableDef | None
        if raw.qualifier is not None:
            qualifier = raw.qualifier.lower()
            table_key = self._aliases.get(qualifier)
            if table_key is None:
                raise QueryError(
                    f"unknown table alias {raw.qualifier!r}",
                    f"sql@{raw.position}",
                    reason="unknown_ref",
                    ref_type="alias",
                )
            table = self.catalog.table(table_key)
        else:
            matches: list[TableDef] = []
            for key in self._referenced_tables:
                candidate = self.catalog.table(key)
                if candidate is not None and candidate.column(raw.column) is not None:
                    matches.append(candidate)
            if not matches:
                raise QueryError(
                    f"unknown column {raw.column!r}",
                    f"sql@{raw.position}",
                    reason="unknown_ref",
                    ref_type="column",
                )
            if len(matches) > 1:
                raise QueryError(
                    f"ambiguous column {raw.column!r}",
                    f"sql@{raw.position}",
                    reason="ambiguous_ref",
                    tables=[table.key for table in matches],
                )
            table = matches[0]
        if table is None:
            raise QueryError("could not resolve table reference", f"sql@{raw.position}", reason="unknown_ref")
        column = table.column(raw.column)
        if column is None:
            raise QueryError(
                f"unknown column {table.key}.{raw.column.lower()}",
                f"sql@{raw.position}",
                reason="unknown_ref",
                ref_type="column",
            )
        return ColumnRef(table.key, column.key)

    def _normalize_predicate(self, raw: _RawPredicate, path: str) -> Predicate:
        if raw.op not in COMPARISON_OPS:
            raise QueryError(f"unsupported operator {raw.op!r}", path)
        left_is_col = isinstance(raw.left, _RawColumn)
        right_is_col = isinstance(raw.right, _RawColumn)
        if not left_is_col and not right_is_col:
            raise QueryError("predicate must reference at least one column", path)

        if left_is_col:
            left = self._resolve_column(raw.left)  # type: ignore[arg-type]
        else:
            left = raw.left
        if right_is_col:
            right = self._resolve_column(raw.right)  # type: ignore[arg-type]
        else:
            right = raw.right
        op = raw.op

        if isinstance(left, _RawColumn) and isinstance(right, Literal):
            # Unreachable after resolution, but keeps the flow explicit.
            left = self._resolve_column(left)
        if isinstance(left, Literal) and isinstance(right, _RawColumn):
            right = self._resolve_column(right)

        assert not isinstance(left, _RawColumn) and not isinstance(right, _RawColumn)

        if isinstance(left, Literal) and isinstance(right, ColumnRef):
            left, right = right, left
            op = FLIP_OP[op]

        if isinstance(right, ColumnRef):
            left_col = self.catalog.column(left.table, left.column)
            right_col = self.catalog.column(right.table, right.column)
            assert left_col is not None and right_col is not None
            if not type_comparable(left_col.type, right_col.type, op):
                raise TypeMismatchError(
                    f"join/comparison types are not comparable: "
                    f"{left_col.type} and {right_col.type} with {op}",
                    path,
                    left=left.render(),
                    right=right.render(),
                )
            if left.key == right.key:
                # A deterministic self-comparison is legal, but sorting it
                # keeps equivalent column orderings identical.
                pass
            if left.key > right.key:
                left, right = right, left
                op = FLIP_OP[op]
        else:
            left_col = self.catalog.column(left.table, left.column)
            assert left_col is not None
            literal_type = right.type
            if not literal_comparable(left_col.type, literal_type, op):
                raise TypeMismatchError(
                    f"literal type {literal_type} is not comparable with column "
                    f"{left.render()} of type {left_col.type}",
                    path,
                    column=left.render(),
                    column_type=left_col.type,
                    literal_type=literal_type,
                )
        return Predicate(left, op, right)


    def _canonical_inner_order(
        self,
        parsed_order: list[str],
        join_predicates: list[Predicate],
    ) -> list[str]:
        """Greedily grow a connected, lexicographically stable INNER order."""

        remaining = set(parsed_order)
        first = min(remaining)
        ordered = [first]
        remaining.remove(first)
        edges = {
            tuple(sorted(predicate.tables))
            for predicate in join_predicates
            if len(predicate.tables) == 2
        }
        while remaining:
            connected = sorted(
                table
                for table in remaining
                if any(tuple(sorted((table, current))) in edges for current in ordered)
            )
            next_table = connected[0] if connected else min(remaining)
            ordered.append(next_table)
            remaining.remove(next_table)
        return ordered

    def _expand_projection(
        self,
        raw_projection: Iterable[_RawColumn | tuple[str, str] | None],
        canonical_order: list[str],
    ) -> tuple[ColumnRef, ...]:
        result: list[ColumnRef] = []
        for item in raw_projection:
            if item is None:
                for table_key in canonical_order:
                    table = self.catalog.table(table_key)
                    assert table is not None
                    result.extend(ColumnRef(table.key, column.key) for column in table.columns)
            elif isinstance(item, tuple):
                qualifier, star = item
                table_key = self._aliases.get(qualifier)
                if table_key is None:
                    raise QueryError(
                        f"unknown table alias {qualifier!r}",
                        "query.projection",
                        reason="unknown_ref",
                    )
                table = self.catalog.table(table_key)
                assert table is not None
                result.extend(ColumnRef(table.key, column.key) for column in table.columns)
            else:
                ref = self._resolve_column(item)
                result.append(ref)
        return tuple(result)

    def _build_canonical_sql(
        self,
        projection: tuple[ColumnRef, ...],
        canonical_order: list[str],
        has_outer: bool,
        edges: tuple[JoinEdge, ...],
        join_predicates: list[Predicate],
        local_filters: dict[str, list[Predicate]],
        post_filters: list[Predicate],
    ) -> str:
        select_text = ", ".join(ref.render() for ref in projection)
        if has_outer:
            from_text = canonical_order[0]
            for edge in edges:
                keyword = "LEFT JOIN" if edge.kind == "left" else "INNER JOIN"
                on_text = " AND ".join(pred.render() for pred in edge.predicates) or "true"
                from_text += f" {keyword} {edge.right_table} ON {on_text}"
        else:
            from_text = canonical_order[0]
            available = {canonical_order[0]}
            emitted: set[int] = set()
            for right_table in canonical_order[1:]:
                new_context = set(available)
                new_context.add(right_table)
                selected = [
                    (index, predicate)
                    for index, predicate in enumerate(join_predicates)
                    if index not in emitted and predicate.tables <= new_context
                ]
                selected.sort(key=lambda item: _predicate_key(item[1]))
                for index, _ in selected:
                    emitted.add(index)
                on_text = " AND ".join(predicate.render() for _, predicate in selected) or "true"
                from_text += f" INNER JOIN {right_table} ON {on_text}"
                available = new_context
            leftovers = [
                predicate for index, predicate in enumerate(join_predicates) if index not in emitted
            ]
            if leftovers:
                post_filters = list(post_filters) + leftovers

        filter_texts: list[str] = []
        for table_key in canonical_order:
            filter_texts.extend(
                predicate.render()
                for predicate in sorted(local_filters.get(table_key, []), key=_predicate_key)
            )
        filter_texts.extend(predicate.render() for predicate in sorted(post_filters, key=_predicate_key))
        where_text = ""
        if filter_texts:
            where_text = " WHERE " + " AND ".join(filter_texts)
        return f"SELECT {select_text} FROM {from_text}{where_text}"


def _predicate_key(predicate: Predicate) -> tuple[str, str, str, str]:
    if isinstance(predicate.right, ColumnRef):
        right = predicate.right.render()
    else:
        right = render_literal(predicate.right)
    return (predicate.left.render(), predicate.op, right, str(predicate.tables))


def parse_query(sql: str, catalog: Catalog) -> Query:
    return QueryParser(catalog).parse(sql)
