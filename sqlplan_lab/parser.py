"""A deliberately small SELECT parser with deterministic normalization.

The supported grammar is intentionally limited to the experiment surface:
simple projections, one two-table inner join, and boolean predicates made of
comparisons and IN lists.  Anything outside that surface becomes ``E_PARSE``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .errors import E_PARSE, LabError
from .util import canonical_json, sql_quote_literal


RESERVED_AFTER_TABLE = {
    "AS",
    "JOIN",
    "INNER",
    "LEFT",
    "RIGHT",
    "FULL",
    "CROSS",
    "ON",
    "WHERE",
    "GROUP",
    "ORDER",
    "LIMIT",
    "HAVING",
    "UNION",
}
AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX", "TOTAL", "GROUP_CONCAT"}


@dataclass(frozen=True)
class Token:
    kind: str
    value: Any
    position: int


@dataclass(frozen=True)
class ColumnRef:
    qualifier: str | None
    name: str

    def canonical(self) -> str:
        if self.qualifier:
            return f"{self.qualifier}.{self.name}"
        return self.name

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "column", "qualifier": self.qualifier, "name": self.name}


@dataclass(frozen=True)
class Literal:
    value: Any
    value_type: str

    def canonical(self) -> str:
        if self.value is None:
            return "NULL"
        if self.value_type == "bool":
            return "TRUE" if self.value else "FALSE"
        return sql_quote_literal(self.value)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "literal", "value_type": self.value_type, "value": self.value}


@dataclass(frozen=True)
class Comparison:
    left: ColumnRef
    op: str
    right: ColumnRef | Literal

    def canonical(self) -> str:
        return f"{self.left.canonical()} {self.op} {self.right.canonical()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "comparison",
            "left": self.left.to_dict(),
            "op": self.op,
            "right": self.right.to_dict(),
        }


@dataclass(frozen=True)
class InList:
    column: ColumnRef
    values: tuple[Literal, ...]

    def canonical(self) -> str:
        values = sorted({value.canonical() for value in self.values})
        return f"{self.column.canonical()} IN ({', '.join(values)})"

    def to_dict(self) -> dict[str, Any]:
        values = {value.canonical(): value for value in self.values}
        return {
            "kind": "in",
            "column": self.column.to_dict(),
            "values": [values[key].to_dict() for key in sorted(values)],
        }


@dataclass(frozen=True)
class BoolExpr:
    op: str
    children: tuple["Condition", ...]

    def canonical(self) -> str:
        pieces: list[str] = []
        for child in sorted(self.children, key=lambda item: item.canonical()):
            text = child.canonical()
            if isinstance(child, BoolExpr):
                text = f"({text})"
            pieces.append(text)
        return f" {self.op.upper()} ".join(pieces)

    def to_dict(self) -> dict[str, Any]:
        children = sorted(self.children, key=lambda item: item.canonical())
        return {
            "kind": "bool",
            "op": self.op,
            "children": [child.to_dict() for child in children],
        }


Condition = Comparison | InList | BoolExpr


@dataclass(frozen=True)
class TableBinding:
    table: str
    alias: str

    def to_dict(self) -> dict[str, str]:
        return {"table": self.table, "alias": self.alias}


@dataclass(frozen=True)
class Query:
    select_items: tuple[str | ColumnRef, ...]
    distinct: bool
    from_table: TableBinding
    join_table: TableBinding | None
    join_condition: Condition | None
    where: Condition | None

    def tables(self) -> tuple[TableBinding, ...]:
        if self.join_table is None:
            return (self.from_table,)
        return (self.from_table, self.join_table)

    def normalized_sql(self) -> str:
        select_parts = []
        for item in self.select_items:
            select_parts.append(item if isinstance(item, str) else item.canonical())
        prefix = "select distinct " if self.distinct else "select "
        parts = [prefix + ", ".join(select_parts)]
        parts.append(f"from {self.from_table.table} as {self.from_table.alias}")
        if self.join_table is not None:
            parts.append(
                f"join {self.join_table.table} as {self.join_table.alias} on "
                f"{self.join_condition.canonical() if self.join_condition else 'true'}"
            )
        if self.where is not None:
            parts.append(f"where {self.where.canonical()}")
        return " ".join(parts)

    def semantic_signature(self) -> str:
        return canonical_json(
            {
                "select": [
                    item if isinstance(item, str) else item.to_dict()
                    for item in self.select_items
                ],
                "distinct": self.distinct,
                "from": self.from_table.to_dict(),
                "join": self.join_table.to_dict() if self.join_table else None,
                "join_condition": self.join_condition.to_dict()
                if self.join_condition
                else None,
                "where": self.where.to_dict() if self.where else None,
            }
        )


class _Lexer:
    def __init__(self, text: str):
        self.text = text
        self.length = len(text)
        self.position = 0

    def tokens(self) -> list[Token]:
        result: list[Token] = []
        while True:
            self._skip_ws_and_comments()
            if self.position >= self.length:
                result.append(Token("EOF", "", self.position))
                return result
            start = self.position
            ch = self.text[self.position]
            nxt = self.text[self.position + 1] if self.position + 1 < self.length else ""
            if ch == "'":
                result.append(self._string_token())
                continue
            if ch == '"':
                result.append(self._quoted_identifier())
                continue
            if ch == "[":
                result.append(self._bracket_identifier())
                continue
            if ch.isdigit() or (ch == "." and nxt.isdigit()):
                result.append(self._number())
                continue
            if ch.isalpha() or ch == "_":
                self.position += 1
                while self.position < self.length:
                    item = self.text[self.position]
                    if item.isalnum() or item in "_$":
                        self.position += 1
                    else:
                        break
                value = self.text[start : self.position].lower()
                result.append(Token("IDENT", value, start))
                continue
            if nxt and ch + nxt in {"<=", ">=", "<>", "!="}:
                self.position += 2
                result.append(Token("OP", ch + nxt, start))
                continue
            if ch in "=<>":
                self.position += 1
                result.append(Token("OP", ch, start))
                continue
            if ch in "(),.*;-":
                self.position += 1
                result.append(Token(ch, ch, start))
                continue
            raise self._error(f"unexpected character {ch!r}", start)

    def _skip_ws_and_comments(self) -> None:
        while self.position < self.length:
            ch = self.text[self.position]
            nxt = self.text[self.position + 1] if self.position + 1 < self.length else ""
            if ch.isspace():
                self.position += 1
                continue
            if ch == "-" and nxt == "-":
                self.position += 2
                while self.position < self.length and self.text[self.position] != "\n":
                    self.position += 1
                continue
            if ch == "/" and nxt == "*":
                end = self.text.find("*/", self.position + 2)
                if end < 0:
                    raise self._error("unterminated block comment", self.position)
                self.position = end + 2
                continue
            return

    def _string_token(self) -> Token:
        start = self.position
        self.position += 1
        chars: list[str] = []
        while self.position < self.length:
            ch = self.text[self.position]
            if ch == "'":
                if self.position + 1 < self.length and self.text[self.position + 1] == "'":
                    chars.append("'")
                    self.position += 2
                    continue
                self.position += 1
                return Token("STRING", "".join(chars), start)
            chars.append(ch)
            self.position += 1
        raise self._error("unterminated string literal", start)

    def _quoted_identifier(self) -> Token:
        start = self.position
        self.position += 1
        chars: list[str] = []
        while self.position < self.length:
            ch = self.text[self.position]
            if ch == '"':
                if self.position + 1 < self.length and self.text[self.position + 1] == '"':
                    chars.append('"')
                    self.position += 2
                    continue
                self.position += 1
                return Token("IDENT", "".join(chars).lower(), start)
            chars.append(ch)
            self.position += 1
        raise self._error("unterminated quoted identifier", start)

    def _bracket_identifier(self) -> Token:
        start = self.position
        self.position += 1
        chars: list[str] = []
        while self.position < self.length:
            ch = self.text[self.position]
            if ch == "]":
                self.position += 1
                return Token("IDENT", "".join(chars).lower(), start)
            chars.append(ch)
            self.position += 1
        raise self._error("unterminated bracket identifier", start)

    def _number(self) -> Token:
        start = self.position
        dot_seen = False
        while self.position < self.length:
            ch = self.text[self.position]
            if ch.isdigit():
                self.position += 1
            elif ch == "." and not dot_seen:
                dot_seen = True
                self.position += 1
            else:
                break
        if self.position < self.length and self.text[self.position] in "eE":
            self.position += 1
            if self.position < self.length and self.text[self.position] in "+-":
                self.position += 1
            exponent_start = self.position
            while self.position < self.length and self.text[self.position].isdigit():
                self.position += 1
            if exponent_start == self.position:
                raise self._error("invalid exponent", start)
        text = self.text[start : self.position]
        if dot_seen or "e" in text.lower():
            return Token("NUMBER", float(text), start)
        return Token("NUMBER", int(text), start)

    def _error(self, message: str, position: int | None = None) -> LabError:
        return LabError(
            E_PARSE,
            message,
            {"position": self.position if position is None else position},
        )


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = _Lexer(text).tokens()
        self.index = 0
        self._check_unsafe_tokens()

    def parse(self) -> Query:
        self._expect_keyword("select")
        distinct = self._accept_keyword("distinct")
        select_items = self._parse_select_items()
        self._expect_keyword("from")
        from_table = self._parse_table_binding()
        join_table: TableBinding | None = None
        join_condition: Condition | None = None
        if self._accept_keyword("inner"):
            self._expect_keyword("join")
            join_table = self._parse_table_binding()
            self._expect_keyword("on")
            join_condition = self._parse_condition()
        elif self._accept_keyword("join"):
            join_table = self._parse_table_binding()
            self._expect_keyword("on")
            join_condition = self._parse_condition()
        if self._peek_keyword("join") or self._peek_keyword("inner"):
            self._fail("only two-table joins are supported")
        if self._peek_keyword("left") or self._peek_keyword("right") or self._peek_keyword("full"):
            self._fail("only inner joins are supported")
        if self._peek_keyword("cross"):
            self._fail("cross joins are not supported")
        where = None
        if self._accept_keyword("where"):
            where = self._parse_condition()
        if self._accept(";"):
            pass
        if not self._at("EOF"):
            self._fail(
                "unsupported trailing SQL clause or expression",
                token=self._current(),
            )
        if join_table is not None and join_condition is None:
            self._fail("join requires an ON condition")
        return Query(
            select_items=tuple(select_items),
            distinct=distinct,
            from_table=from_table,
            join_table=join_table,
            join_condition=join_condition,
            where=where,
        )

    def _check_unsafe_tokens(self) -> None:
        select_seen = False
        for index, token in enumerate(self.tokens):
            upper = str(token.value).upper() if token.kind == "IDENT" else ""
            if upper == "SELECT":
                if select_seen:
                    raise LabError(
                        E_PARSE,
                        "subqueries are not supported",
                        {"position": token.position},
                    )
                select_seen = True
            if upper in {"GROUP", "HAVING", "UNION", "INTERSECT", "EXCEPT"}:
                raise LabError(
                    E_PARSE,
                    f"{upper} is not supported",
                    {"position": token.position},
                )
            if upper in AGGREGATES and index + 1 < len(self.tokens):
                nxt = self.tokens[index + 1]
                if nxt.kind == "(":
                    raise LabError(
                        E_PARSE,
                        "aggregate functions are not supported",
                        {"position": token.position},
                    )

    def _parse_select_items(self) -> list[str | ColumnRef]:
        items: list[str | ColumnRef] = []
        while True:
            if self._accept("*"):
                items.append("*")
            elif (
                self._current().kind == "IDENT"
                and self._peek_index(1, ".")
                and self._peek_index(2, "*")
            ):
                qualifier = self._parse_identifier("table alias")
                self._expect(".")
                self._expect("*")
                items.append(f"{qualifier}.*")
            else:
                column = self._parse_column()
                if self._peek("("):
                    self._fail("expressions and functions are not supported")
                items.append(column)
            if not self._accept(","):
                break
        if not items:
            self._fail("SELECT list cannot be empty")
        return items

    def _parse_table_binding(self) -> TableBinding:
        table = self._parse_identifier("table name")
        alias = table
        if self._accept_keyword("as"):
            alias = self._parse_identifier("table alias")
        elif self._current().kind == "IDENT":
            value = str(self._current().value)
            if value.upper() not in RESERVED_AFTER_TABLE:
                self.index += 1
                alias = value
        return TableBinding(table=table, alias=alias)

    def _parse_condition(self) -> Condition:
        return self._parse_or()

    def _parse_or(self) -> Condition:
        children = [self._parse_and()]
        while self._accept_keyword("or"):
            children.append(self._parse_and())
        return self._make_bool("or", children)

    def _parse_and(self) -> Condition:
        children = [self._parse_condition_primary()]
        while self._accept_keyword("and"):
            children.append(self._parse_condition_primary())
        return self._make_bool("and", children)

    def _parse_condition_primary(self) -> Condition:
        if self._accept("("):
            condition = self._parse_or()
            self._expect(")")
            return condition
        left = self._parse_column()
        if self._peek("("):
            self._fail("functions and subqueries are not supported")
        if self._accept_keyword("in"):
            self._expect("(")
            values: list[Literal] = []
            while True:
                literal = self._parse_literal()
                if literal is None:
                    self._fail("IN lists may contain literals only")
                values.append(literal)
                if not self._accept(","):
                    break
            self._expect(")")
            if not values:
                self._fail("IN list cannot be empty")
            return InList(column=left, values=tuple(values))
        if self._accept_keyword("not"):
            self._fail("NOT IN and NOT predicates are not supported")
        if not self._peek_op():
            self._fail("expected a comparison operator or IN")
        op = str(self._current().value)
        self.index += 1
        right_literal = self._parse_literal()
        if right_literal is not None:
            return Comparison(left=left, op=op, right=right_literal)
        right_column = self._parse_column()
        return Comparison(left=left, op=op, right=right_column)

    def _parse_literal(self) -> Literal | None:
        token = self._current()
        if token.kind == "STRING":
            self.index += 1
            return Literal(token.value, "str")
        if token.kind == "NUMBER":
            self.index += 1
            return Literal(token.value, "float" if isinstance(token.value, float) else "int")
        if token.kind == "-" and self._peek_index(1, "NUMBER"):
            self.index += 1
            number = self._current()
            self.index += 1
            value = -number.value
            return Literal(value, "float" if isinstance(value, float) else "int")
        if token.kind == "IDENT":
            upper = str(token.value).upper()
            if upper == "NULL":
                self.index += 1
                return Literal(None, "null")
            if upper in {"TRUE", "FALSE"}:
                self.index += 1
                return Literal(upper == "TRUE", "bool")
        return None

    def _parse_column(self) -> ColumnRef:
        if self._peek_keyword("select"):
            self._fail("subqueries are not supported")
        first = self._parse_identifier("column name")
        qualifier: str | None = None
        name = first
        if self._accept("."):
            qualifier = first
            name = self._parse_identifier("column name")
        return ColumnRef(qualifier=qualifier, name=name)

    def _parse_identifier(self, label: str) -> str:
        token = self._current()
        if token.kind != "IDENT":
            self._fail(f"expected {label}", token=token)
        value = str(token.value).lower()
        if value == "select":
            self._fail("subqueries are not supported", token=token)
        self.index += 1
        return value

    def _make_bool(self, op: str, children: Iterable[Condition]) -> Condition:
        flattened: list[Condition] = []
        for child in children:
            if isinstance(child, BoolExpr) and child.op == op:
                flattened.extend(child.children)
            else:
                flattened.append(child)
        if len(flattened) == 1:
            return flattened[0]
        unique = {child.canonical(): child for child in flattened}
        return BoolExpr(op=op, children=tuple(unique[key] for key in sorted(unique)))

    def _current(self) -> Token:
        return self.tokens[self.index]

    def _peek_index(self, offset: int, kind: str) -> bool:
        position = self.index + offset
        return position < len(self.tokens) and self.tokens[position].kind == kind

    def _peek(self, kind: str) -> bool:
        return self._current().kind == kind

    def _accept(self, kind: str) -> bool:
        if self._peek(kind):
            self.index += 1
            return True
        return False

    def _expect(self, kind: str) -> Token:
        token = self._current()
        if token.kind != kind:
            self._fail(f"expected {kind!r}", token=token)
        self.index += 1
        return token

    def _peek_keyword(self, keyword: str) -> bool:
        token = self._current()
        return token.kind == "IDENT" and str(token.value).upper() == keyword.upper()

    def _accept_keyword(self, keyword: str) -> bool:
        if self._peek_keyword(keyword):
            self.index += 1
            return True
        return False

    def _expect_keyword(self, keyword: str) -> None:
        if not self._accept_keyword(keyword):
            self._fail(f"expected keyword {keyword.upper()}")

    def _peek_op(self) -> bool:
        return self._current().kind == "OP"

    def _at(self, kind: str) -> bool:
        return self._current().kind == kind

    def _fail(self, message: str, token: Token | None = None) -> None:
        current = token or self._current()
        raise LabError(E_PARSE, message, {"position": current.position})


def parse_sql(text: str) -> Query:
    if not isinstance(text, str) or not text.strip():
        raise LabError(E_PARSE, "query is empty")
    try:
        return _Parser(text).parse()
    except LabError:
        raise
    except Exception as exc:  # pragma: no cover - defensive conversion
        raise LabError(E_PARSE, f"could not parse query: {exc}") from exc


def parse_query_file(path: str) -> Query:
    from pathlib import Path

    return parse_sql(Path(path).read_text(encoding="utf-8"))


def condition_to_sql(condition: Condition | None) -> str:
    if condition is None:
        return "1 = 1"
    if isinstance(condition, BoolExpr):
        rendered = []
        for child in sorted(condition.children, key=lambda item: item.canonical()):
            text = condition_to_sql(child)
            if isinstance(child, BoolExpr):
                text = f"({text})"
            rendered.append(text)
        return f" {condition.op.upper()} ".join(rendered)
    if isinstance(condition, Comparison):
        left = _column_to_sql(condition.left)
        right = (
            _column_to_sql(condition.right)
            if isinstance(condition.right, ColumnRef)
            else sql_quote_literal(condition.right.value)
        )
        return f"{left} {condition.op} {right}"
    if isinstance(condition, InList):
        values = sorted({value.canonical(): value for value in condition.values}.values(), key=lambda item: item.canonical())
        return f"{_column_to_sql(condition.column)} IN ({', '.join(sql_quote_literal(item.value) for item in values)})"
    raise LabError(E_PARSE, "unknown condition node")


def _column_to_sql(column: ColumnRef) -> str:
    name = '"' + column.name.replace('"', '""') + '"'
    if column.qualifier:
        return '"' + column.qualifier.replace('"', '""') + '".' + name
    return name


def iter_columns(condition: Condition | None) -> Iterable[ColumnRef]:
    if condition is None:
        return
    if isinstance(condition, Comparison):
        yield condition.left
        if isinstance(condition.right, ColumnRef):
            yield condition.right
        return
    if isinstance(condition, InList):
        yield condition.column
        return
    for child in condition.children:
        yield from iter_columns(child)


def condition_columns(condition: Condition | None) -> tuple[ColumnRef, ...]:
    unique = {column.canonical(): column for column in iter_columns(condition)}
    return tuple(unique[key] for key in sorted(unique))
