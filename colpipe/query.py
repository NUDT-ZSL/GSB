"""SQL-subset lexer, parser and schema binder.

Supported shape:

    SELECT <group refs | aggregate calls> [AS alias], ...
    FROM <table> [AS] <alias>
    INNER JOIN <table> [AS] <alias> ON <equi-join conjuncts>
    [WHERE <predicate> AND ...]
    [GROUP BY <ref>, ...]
    [ORDER BY <output alias | group ref> [ASC|DESC], ...]

The grammar deliberately rejects OR, subqueries, more than two relation
inputs, non-equality joins and aggregate DISTINCT with E_PARSE.  Parentheses
inside an AND expression are flattened, and predicates are canonicalised, so
formatting and redundant grouping do not alter the plan summary.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .catalog import Column, Table
from .codes import E_DATA, E_PARSE
from .errors import PipeError
from .util import canonical_json, sha256_json

AGGREGATES = frozenset({"SUM", "COUNT", "AVG", "MIN", "MAX"})
RESERVED = frozenset(
    {
        "SELECT",
        "FROM",
        "INNER",
        "JOIN",
        "ON",
        "WHERE",
        "GROUP",
        "BY",
        "ORDER",
        "AS",
        "AND",
        "OR",
        "IN",
        "NOT",
        "IS",
        "NULL",
        "BETWEEN",
        "ASC",
        "DESC",
        "TRUE",
        "FALSE",
    }
)


@dataclass(frozen=True)
class Token:
    kind: str
    value: Any
    offset: int
    line: int
    column: int


class Lexer:
    def __init__(self, text: str, path: str) -> None:
        self.text = text
        self.path = path
        self.length = len(text)
        self.pos = 0
        self.line = 1
        self.column = 1

    def _error(self, message: str, line: int | None = None, column: int | None = None) -> PipeError:
        line = self.line if line is None else line
        column = self.column if column is None else column
        return PipeError(
            E_PARSE,
            message,
            f"{self.path}:{line}:{column}",
            {"line": line, "column": column, "offset": self.pos},
        )

    def _advance(self, count: int = 1) -> None:
        for _ in range(count):
            if self.pos >= self.length:
                return
            char = self.text[self.pos]
            self.pos += 1
            if char == "\n":
                self.line += 1
                self.column = 1
            else:
                self.column += 1

    def _peek(self, offset: int = 0) -> str:
        index = self.pos + offset
        return self.text[index] if index < self.length else ""

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while self.pos < self.length:
            char = self._peek()
            if char.isspace():
                self._advance()
                continue
            if char == "-" and self._peek(1) == "-":
                while self.pos < self.length and self._peek() != "\n":
                    self._advance()
                continue
            line, column, offset = self.line, self.column, self.pos
            if char.isalpha() or char == "_":
                start = self.pos
                while self._peek().isalnum() or self._peek() == "_":
                    self._advance()
                tokens.append(Token("IDENT", self.text[start:self.pos], offset, line, column))
                continue
            if char.isdigit() or (char == "-" and self._peek(1).isdigit()):
                tokens.append(self._read_number())
                continue
            if char in ("'", '"'):
                tokens.append(self._read_string())
                continue
            two = self.text[self.pos : self.pos + 2]
            if two in {"<=", ">=", "<>", "!=", "=="}:
                self._advance(2)
                tokens.append(Token("SYMBOL", two, offset, line, column))
                continue
            if char in "(),.[]*;=<>":
                self._advance()
                tokens.append(Token("SYMBOL", char, offset, line, column))
                continue
            raise self._error(f"unsupported character {char!r}")
        tokens.append(Token("EOF", None, self.pos, self.line, self.column))
        return tokens

    def _read_number(self) -> Token:
        line, column, offset = self.line, self.column, self.pos
        start = self.pos
        if self._peek() == "-":
            self._advance()
        while self._peek().isdigit():
            self._advance()
        is_float = False
        if self._peek() == ".":
            is_float = True
            self._advance()
            if not self._peek().isdigit():
                raise self._error("expected digits after decimal point")
            while self._peek().isdigit():
                self._advance()
        if self._peek().lower() == "e":
            is_float = True
            self._advance()
            if self._peek() in "+-":
                self._advance()
            if not self._peek().isdigit():
                raise self._error("expected exponent digits")
            while self._peek().isdigit():
                self._advance()
        raw = self.text[start:self.pos]
        try:
            value: int | float = float(raw) if is_float else int(raw)
        except ValueError as exc:  # pragma: no cover - scanner is defensive
            raise self._error("invalid numeric literal", line, column) from exc
        if isinstance(value, float) and not math.isfinite(value):
            raise self._error("numeric literal must be finite", line, column)
        return Token("NUMBER", value, offset, line, column)

    def _read_string(self) -> Token:
        line, column, offset = self.line, self.column, self.pos
        quote = self._peek()
        self._advance()
        chars: list[str] = []
        while self.pos < self.length:
            char = self._peek()
            if char == quote:
                if self._peek(1) == quote:
                    chars.append(quote)
                    self._advance(2)
                    continue
                self._advance()
                return Token("STRING", "".join(chars), offset, line, column)
            if char == "\\":
                self._advance()
                escaped = self._peek()
                escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", "'": "'", '"': '"'}
                if escaped in escapes:
                    chars.append(escapes[escaped])
                    self._advance()
                    continue
                raise self._error(f"unsupported escape sequence \\{escaped}")
            chars.append(char)
            self._advance()
        raise self._error("unterminated string literal", line, column)


@dataclass(frozen=True)
class RawRef:
    qualifier: str | None
    name: str

    @property
    def text(self) -> str:
        return f"{self.qualifier}.{self.name}" if self.qualifier else self.name


@dataclass(frozen=True)
class RawSelectItem:
    kind: str
    ref: RawRef | None
    func: str | None
    star: bool
    alias: str | None


@dataclass(frozen=True)
class RawPredicate:
    ref: RawRef
    op: str
    values: tuple[Any, ...]


@dataclass(frozen=True)
class RawOrderItem:
    ref: RawRef
    direction: str


@dataclass(frozen=True)
class RawQuery:
    select: tuple[RawSelectItem, ...]
    from_table: str
    from_alias: str
    join_table: str
    join_alias: str
    join_pairs: tuple[tuple[RawRef, RawRef], ...]
    where: tuple[RawPredicate, ...]
    group_by: tuple[RawRef, ...]
    order_by: tuple[RawOrderItem, ...]


class Parser:
    def __init__(self, text: str, path: str) -> None:
        self.text = text
        self.path = path
        self.tokens = Lexer(text, path).tokenize()
        self.index = 0

    @property
    def token(self) -> Token:
        return self.tokens[self.index]

    def peek(self, offset: int = 1) -> Token:
        index = min(self.index + offset, len(self.tokens) - 1)
        return self.tokens[index]

    def _error(self, message: str, token: Token | None = None) -> PipeError:
        token = self.token if token is None else token
        return PipeError(
            E_PARSE,
            message,
            f"{self.path}:{token.line}:{token.column}",
            {"line": token.line, "column": token.column, "offset": token.offset},
        )

    def _is_kw(self, word: str, token: Token | None = None) -> bool:
        token = self.token if token is None else token
        return token.kind == "IDENT" and str(token.value).upper() == word

    def _match_kw(self, word: str) -> bool:
        if self._is_kw(word):
            self.index += 1
            return True
        return False

    def _expect_kw(self, word: str) -> None:
        if not self._match_kw(word):
            raise self._error(f"expected {word}")

    def _is_symbol(self, symbol: str, token: Token | None = None) -> bool:
        token = self.token if token is None else token
        return token.kind == "SYMBOL" and token.value == symbol

    def _match_symbol(self, symbol: str) -> bool:
        if self._is_symbol(symbol):
            self.index += 1
            return True
        return False

    def _expect_symbol(self, symbol: str) -> None:
        if not self._match_symbol(symbol):
            raise self._error(f"expected {symbol!r}")

    def _expect_ident(self, what: str = "identifier", *, allow_reserved: bool = False) -> tuple[str, Token]:
        token = self.token
        if token.kind != "IDENT":
            raise self._error(f"expected {what}")
        value = str(token.value)
        if not allow_reserved and value.upper() in RESERVED:
            raise self._error(f"{what} cannot be a reserved word")
        self.index += 1
        return value, token

    def parse(self) -> RawQuery:
        self._expect_kw("SELECT")
        select: list[RawSelectItem] = [self._parse_select_item()]
        while self._match_symbol(","):
            select.append(self._parse_select_item())
        self._expect_kw("FROM")
        from_table, from_alias = self._parse_table_ref()
        if self._match_kw("INNER"):
            self._expect_kw("JOIN")
        else:
            self._expect_kw("JOIN")
        join_table, join_alias = self._parse_table_ref()
        self._expect_kw("ON")
        join_pairs = list(self._parse_join_condition())
        if self._is_kw("JOIN") or self._is_kw("INNER"):
            raise self._error("exactly two table inputs are supported")

        where: list[RawPredicate] = []
        if self._match_kw("WHERE"):
            where = self._parse_and_expression()

        group_by: list[RawRef] = []
        if self._match_kw("GROUP"):
            self._expect_kw("BY")
            group_by.append(self._parse_ref())
            while self._match_symbol(","):
                group_by.append(self._parse_ref())

        order_by: list[RawOrderItem] = []
        if self._match_kw("ORDER"):
            self._expect_kw("BY")
            order_by.append(self._parse_order_item())
            while self._match_symbol(","):
                order_by.append(self._parse_order_item())

        if self._match_symbol(";"):
            pass
        if self.token.kind != "EOF":
            if self._is_kw("OR"):
                raise self._error("OR is not supported; use independent AND predicates")
            raise self._error(f"unexpected token {self.token.value!r}")
        return RawQuery(
            tuple(select),
            from_table,
            from_alias,
            join_table,
            join_alias,
            tuple(join_pairs),
            tuple(where),
            tuple(group_by),
            tuple(order_by),
        )

    def _parse_select_item(self) -> RawSelectItem:
        if (
            self.token.kind == "IDENT"
            and str(self.token.value).upper() in AGGREGATES
            and self._is_symbol("(", self.peek())
        ):
            func = str(self.token.value).upper()
            self.index += 1
            self._expect_symbol("(")
            star = self._match_symbol("*")
            ref = None if star else self._parse_ref()
            self._expect_symbol(")")
            alias = None
            if self._match_kw("AS"):
                alias, _ = self._expect_ident("select alias")
            return RawSelectItem("agg", ref, func, star, alias)
        ref = self._parse_ref()
        alias = None
        if self._match_kw("AS"):
            alias, _ = self._expect_ident("select alias")
        return RawSelectItem("group", ref, None, False, alias)

    def _parse_table_ref(self) -> tuple[str, str]:
        table, _ = self._expect_ident("table name", allow_reserved=True)
        if table.upper() in RESERVED:
            raise self._error(f"table name cannot be a reserved word: {table}")
        alias = table
        if self._match_kw("AS"):
            alias, _ = self._expect_ident("table alias")
        elif self.token.kind == "IDENT" and str(self.token.value).upper() not in RESERVED:
            alias, _ = self._expect_ident("table alias")
        return table, alias

    def _parse_ref(self) -> RawRef:
        first, _ = self._expect_ident("column reference", allow_reserved=False)
        if self._match_symbol("."):
            second, _ = self._expect_ident("column name")
            return RawRef(first, second)
        return RawRef(None, first)

    def _parse_join_condition(self) -> list[tuple[RawRef, RawRef]]:
        pairs = list(self._parse_join_factor())
        while self._match_kw("AND"):
            pairs.extend(self._parse_join_factor())
        return pairs

    def _parse_join_factor(self) -> list[tuple[RawRef, RawRef]]:
        if self._match_symbol("("):
            nested = self._parse_join_condition()
            self._expect_symbol(")")
            return nested
        return [self._parse_join_atom()]

    def _parse_join_atom(self) -> tuple[RawRef, RawRef]:
        left = self._parse_ref()
        operator = self.token.value
        if operator not in {"=", "=="}:
            raise self._error("join predicates must be equality comparisons")
        self.index += 1
        right = self._parse_ref()
        return left, right

    def _parse_and_expression(self) -> list[RawPredicate]:
        predicates = list(self._parse_and_factor())
        while self._match_kw("AND"):
            predicates.extend(self._parse_and_factor())
        return predicates

    def _parse_and_factor(self) -> list[RawPredicate]:
        if self._match_symbol("("):
            nested = self._parse_and_expression()
            self._expect_symbol(")")
            return nested
        return [self._parse_predicate()]

    def _parse_predicate(self) -> RawPredicate:
        ref = self._parse_ref()
        negated = self._match_kw("NOT")
        if self._match_kw("IN"):
            opening = self.token.value
            if opening not in {"(", "["}:
                raise self._error("IN requires a parenthesised list")
            closing = ")" if opening == "(" else "]"
            self.index += 1
            values = [self._parse_literal()]
            while self._match_symbol(","):
                values.append(self._parse_literal())
            self._expect_symbol(closing)
            return RawPredicate(ref, "not_in" if negated else "in", tuple(values))
        if self._match_kw("BETWEEN"):
            low = self._parse_literal()
            self._expect_kw("AND")
            high = self._parse_literal()
            return RawPredicate(ref, "not_between" if negated else "between", (low, high))
        if negated:
            raise self._error("NOT is only supported with IN or BETWEEN")
        if self._match_kw("IS"):
            is_not = self._match_kw("NOT")
            self._expect_kw("NULL")
            return RawPredicate(ref, "is_not_null" if is_not else "is_null", ())
        operator = self.token.value
        if operator not in {"=", "==", "!=", "<>", "<", "<=", ">", ">="}:
            raise self._error("expected a comparison operator, IN, BETWEEN or IS NULL")
        self.index += 1
        value = self._parse_literal()
        normalized = "!=" if operator == "<>" else "=" if operator == "==" else operator
        return RawPredicate(ref, normalized, (value,))

    def _parse_literal(self) -> tuple[str, Any]:
        token = self.token
        if token.kind == "NUMBER":
            self.index += 1
            return ("number", token.value)
        if token.kind == "STRING":
            self.index += 1
            return ("string", token.value)
        if self._match_kw("TRUE"):
            return ("bool", True)
        if self._match_kw("FALSE"):
            return ("bool", False)
        if self._match_kw("NULL"):
            return ("null", None)
        raise self._error("expected a literal value")

    def _parse_order_item(self) -> RawOrderItem:
        ref = self._parse_ref()
        direction = "asc"
        if self._match_kw("ASC"):
            direction = "asc"
        elif self._match_kw("DESC"):
            direction = "desc"
        return RawOrderItem(ref, direction)


def parse_query(text: str, *, path: str = "$.query") -> RawQuery:
    if not isinstance(text, str) or not text.strip():
        raise PipeError(E_PARSE, "query must be a non-empty string", path)
    try:
        return Parser(text, path).parse()
    except PipeError:
        raise
    except Exception as exc:  # defensive: malformed syntax must not leak a traceback
        raise PipeError(E_PARSE, f"unable to parse query: {exc}", path) from exc


@dataclass(frozen=True)
class BoundRef:
    alias: str
    column: str
    type: str

    @property
    def text(self) -> str:
        return f"{self.alias}.{self.column}"

    def to_dict(self) -> dict[str, str]:
        return {"ref": self.text, "type": self.type}


@dataclass(frozen=True)
class Predicate:
    ref: BoundRef
    op: str
    values: tuple[Any, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "column": self.ref.text,
            "type": self.ref.type,
            "op": self.op,
            "values": list(self.values),
        }

    @property
    def sort_key(self) -> str:
        return canonical_json(self.to_dict())


@dataclass(frozen=True)
class SelectItem:
    kind: str
    alias: str
    type: str
    nullable: bool
    ref: BoundRef | None = None
    func: str | None = None
    arg: BoundRef | None = None
    star: bool = False

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "alias": self.alias,
            "type": self.type,
            "nullable": self.nullable,
        }
        if self.kind == "group":
            result["expr"] = self.ref.text if self.ref else None
        else:
            result.update(
                {
                    "func": self.func,
                    "arg": self.arg.text if self.arg else "*" if self.star else None,
                    "star": self.star,
                }
            )
        return result


@dataclass(frozen=True)
class JoinPair:
    left: BoundRef
    right: BoundRef

    def to_dict(self) -> dict[str, str]:
        return {"left": self.left.text, "right": self.right.text}


@dataclass(frozen=True)
class OrderTerm:
    target: str
    direction: str
    output_name: str | None = None
    group_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"target": self.target, "direction": self.direction}
        if self.output_name is not None:
            result["output"] = self.output_name
        if self.group_index is not None:
            result["group_index"] = self.group_index
        return result


@dataclass(frozen=True)
class BoundQuery:
    from_table: str
    from_alias: str
    join_table: str
    join_alias: str
    join_pairs: tuple[JoinPair, ...]
    where: tuple[Predicate, ...]
    select: tuple[SelectItem, ...]
    group_by: tuple[BoundRef, ...]
    order_by: tuple[OrderTerm, ...]

    @property
    def aliases(self) -> tuple[str, str]:
        return (self.from_alias, self.join_alias)

    @property
    def table_for_alias(self) -> dict[str, str]:
        return {self.from_alias: self.from_table, self.join_alias: self.join_table}

    @property
    def aggregate_items(self) -> tuple[SelectItem, ...]:
        return tuple(item for item in self.select if item.kind == "agg")

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "from": {"table": self.from_table, "alias": self.from_alias},
            "join": {
                "table": self.join_table,
                "alias": self.join_alias,
                "on": [pair.to_dict() for pair in self.join_pairs],
            },
            "where": [predicate.to_dict() for predicate in self.where],
            "select": [item.to_dict() for item in self.select],
            "group_by": [ref.to_dict() for ref in self.group_by],
            "order_by": [term.to_dict() for term in self.order_by],
        }

    @property
    def canonical_summary(self) -> str:
        return canonical_json(self.canonical_dict())

    @property
    def summary_sha256(self) -> str:
        return sha256_json(self.canonical_dict())

    @property
    def input_refs(self) -> tuple[BoundRef, ...]:
        refs: list[BoundRef] = list(self.group_by)
        for item in self.aggregate_items:
            if item.arg is not None:
                refs.append(item.arg)
        return tuple(refs)


def _semantic_error(message: str, path: str, **details: Any) -> PipeError:
    return PipeError(E_DATA, message, path, {"query_error": message, **details})


class Binder:
    def __init__(self, raw: RawQuery, tables: Mapping[str, Table], path: str) -> None:
        self.raw = raw
        self.tables = tables
        self.path = path
        if raw.from_table not in tables:
            raise _semantic_error(
                f"unknown table {raw.from_table!r}",
                f"{path}.from.table",
                table=raw.from_table,
            )
        if raw.join_table not in tables:
            raise _semantic_error(
                f"unknown table {raw.join_table!r}",
                f"{path}.join.table",
                table=raw.join_table,
            )
        if raw.from_alias == raw.join_alias:
            raise _semantic_error("table aliases must be unique", f"{path}.join.alias")
        self.aliases: dict[str, Table] = {
            raw.from_alias: tables[raw.from_table],
            raw.join_alias: tables[raw.join_table],
        }

    def _resolve(self, ref: RawRef, context: str) -> BoundRef:
        if ref.qualifier is not None:
            table = self.aliases.get(ref.qualifier)
            if table is None:
                raise _semantic_error(
                    f"unknown table alias {ref.qualifier!r}",
                    f"{self.path}.{context}",
                    ref=ref.text,
                )
            try:
                column = table.column(ref.name)
            except KeyError as exc:
                raise _semantic_error(
                    f"unknown column {ref.name!r} on alias {ref.qualifier!r}",
                    f"{self.path}.{context}",
                    ref=ref.text,
                ) from exc
            return BoundRef(ref.qualifier, column.name, column.type)

        matches: list[tuple[str, Column]] = []
        for alias, table in self.aliases.items():
            for column in table.columns:
                if column.name == ref.name:
                    matches.append((alias, column))
        if not matches:
            raise _semantic_error(
                f"unknown column {ref.name!r}",
                f"{self.path}.{context}",
                ref=ref.text,
            )
        if len(matches) > 1:
            raise _semantic_error(
                f"ambiguous unqualified column {ref.name!r}; use a table alias",
                f"{self.path}.{context}",
                ref=ref.text,
                candidates=[alias for alias, _ in matches],
            )
        alias, column = matches[0]
        return BoundRef(alias, column.name, column.type)

    def _coerce(self, literal: tuple[str, Any], ref: BoundRef, context: str) -> Any:
        kind, value = literal
        if kind == "null":
            raise _semantic_error(
                "NULL literals are only valid with IS [NOT] NULL",
                f"{self.path}.{context}",
                ref=ref.text,
            )
        if ref.type == "int64":
            if kind != "number" or isinstance(value, bool) or not isinstance(value, int):
                raise _semantic_error(
                    "int64 comparisons require integer literals",
                    f"{self.path}.{context}",
                    ref=ref.text,
                )
            if value < -(2**63) or value > 2**63 - 1:
                raise _semantic_error(
                    "int64 literal is outside the signed 64-bit range",
                    f"{self.path}.{context}",
                    ref=ref.text,
                )
            return value
        if ref.type == "float64":
            if kind != "number" or isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _semantic_error(
                    "float64 comparisons require numeric literals",
                    f"{self.path}.{context}",
                    ref=ref.text,
                )
            converted = float(value)
            if not math.isfinite(converted):
                raise _semantic_error(
                    "float64 comparison literal must be finite",
                    f"{self.path}.{context}",
                    ref=ref.text,
                )
            return converted
        if ref.type == "bool":
            if kind != "bool":
                raise _semantic_error(
                    "bool comparisons require true or false",
                    f"{self.path}.{context}",
                    ref=ref.text,
                )
            return bool(value)
        if ref.type == "string":
            if kind != "string":
                raise _semantic_error(
                    "string comparisons require quoted literals",
                    f"{self.path}.{context}",
                    ref=ref.text,
                )
            return str(value)
        raise _semantic_error("unsupported comparison type", f"{self.path}.{context}")

    def bind(self) -> BoundQuery:
        raw = self.raw
        join_pairs: list[JoinPair] = []
        for index, (raw_left, raw_right) in enumerate(raw.join_pairs):
            left = self._resolve(raw_left, f"join.on[{index}].left")
            right = self._resolve(raw_right, f"join.on[{index}].right")
            if left.alias == right.alias:
                raise _semantic_error(
                    "each join equality must connect the two table aliases",
                    f"{self.path}.join.on[{index}]",
                )
            if left.type != right.type:
                raise _semantic_error(
                    f"join key type mismatch: {left.text} ({left.type}) vs {right.text} ({right.type})",
                    f"{self.path}.join.on[{index}]",
                )
            join_pairs.append(JoinPair(left, right))
        if not join_pairs:
            raise _semantic_error("an inner join requires at least one equality", f"{self.path}.join")
        join_pairs.sort(key=lambda pair: canonical_json(pair.to_dict()))

        predicates: list[Predicate] = []
        for index, raw_predicate in enumerate(raw.where):
            ref = self._resolve(raw_predicate.ref, f"where[{index}]")
            op = raw_predicate.op
            values: list[Any] = []
            if op in {"is_null", "is_not_null"}:
                if ref.type == "bool":
                    pass
            elif op in {"in", "not_in"}:
                if not raw_predicate.values:
                    raise _semantic_error("IN list must not be empty", f"{self.path}.where[{index}]")
                coerced = [self._coerce(value, ref, f"where[{index}].values") for value in raw_predicate.values]
                values = sorted(set(coerced), key=lambda value: canonical_json(value))
            elif op in {"between", "not_between"}:
                if ref.type == "bool":
                    raise _semantic_error("range filters are not supported on bool", f"{self.path}.where[{index}]")
                low = self._coerce(raw_predicate.values[0], ref, f"where[{index}].values[0]")
                high = self._coerce(raw_predicate.values[1], ref, f"where[{index}].values[1]")
                if low > high:
                    raise _semantic_error("BETWEEN lower bound exceeds upper bound", f"{self.path}.where[{index}]")
                values = [low, high]
            else:
                if op not in {"=", "!=", "<", "<=", ">", ">="}:
                    raise _semantic_error(f"unsupported predicate operator {op!r}", f"{self.path}.where[{index}]")
                if ref.type == "bool" and op not in {"=", "!="}:
                    raise _semantic_error("bool supports only = and !=", f"{self.path}.where[{index}]")
                values = [self._coerce(raw_predicate.values[0], ref, f"{self.path}.where[{index}].value")]
            predicates.append(Predicate(ref, op, tuple(values)))
        predicates.sort(key=lambda predicate: predicate.sort_key)

        group_by: list[BoundRef] = []
        group_keys: set[str] = set()
        for index, raw_ref in enumerate(raw.group_by):
            ref = self._resolve(raw_ref, f"group_by[{index}]")
            if ref.text not in group_keys:
                group_keys.add(ref.text)
                group_by.append(ref)

        select: list[SelectItem] = []
        aliases: set[str] = set()
        for index, raw_item in enumerate(raw.select):
            if raw_item.kind == "agg":
                func = str(raw_item.func)
                if func == "COUNT" and raw_item.star:
                    arg = None
                    type_name = "int64"
                    nullable = False
                    default_alias = "count"
                else:
                    if raw_item.ref is None:
                        raise _semantic_error("aggregate requires a column argument", f"{self.path}.select[{index}]")
                    arg = self._resolve(raw_item.ref, f"select[{index}]")
                    if arg.type not in {"int64", "float64"} and func in {"SUM", "AVG"}:
                        raise _semantic_error(
                            f"{func} requires a numeric column",
                            f"{self.path}.select[{index}]",
                        )
                    if func == "SUM":
                        type_name = arg.type
                    elif func == "AVG":
                        type_name = "float64"
                    elif func == "COUNT":
                        type_name = "int64"
                    else:
                        type_name = arg.type
                    source_column = self.aliases[arg.alias].column(arg.column)
                    # SUM/AVG/MIN/MAX can be NULL when no non-NULL input value
                    # participates (including an empty global aggregate).
                    nullable = func != "COUNT"
                    default_alias = f"{func.lower()}_{arg.column}"
                alias = raw_item.alias or default_alias
            else:
                if raw_item.ref is None:
                    raise _semantic_error("missing select reference", f"{self.path}.select[{index}]")
                ref = self._resolve(raw_item.ref, f"select[{index}]")
                if ref.text not in group_keys:
                    raise _semantic_error(
                        f"non-aggregate selection {ref.text} must appear in GROUP BY",
                        f"{self.path}.select[{index}]",
                    )
                alias = raw_item.alias or ref.column
                source_column = self.aliases[ref.alias].column(ref.column)
                select.append(
                    SelectItem(
                        "group",
                        alias,
                        ref.type,
                        source_column.nullable,
                        ref=ref,
                    )
                )
                if alias in aliases:
                    raise _semantic_error(f"duplicate output alias {alias!r}", f"{self.path}.select[{index}].alias")
                aliases.add(alias)
                continue
            if not alias:
                raise _semantic_error("output alias must be non-empty", f"{self.path}.select[{index}].alias")
            if alias.upper() in RESERVED or not alias[:1].isalpha() and alias[:1] != "_" or not all(
                char.isalnum() or char == "_" for char in alias
            ):
                raise _semantic_error(f"invalid output alias {alias!r}", f"{self.path}.select[{index}].alias")
            if alias in aliases:
                raise _semantic_error(f"duplicate output alias {alias!r}", f"{self.path}.select[{index}].alias")
            aliases.add(alias)
            select.append(
                SelectItem(
                    "agg",
                    alias,
                    type_name,
                    nullable,
                    func=func,
                    arg=arg,
                    star=func == "COUNT" and raw_item.star,
                )
            )

        if not any(item.kind == "agg" for item in select):
            raise _semantic_error("the pipeline requires at least one final aggregate", f"{self.path}.select")

        output_positions = {item.alias: index for index, item in enumerate(select)}
        group_positions = {ref.text: index for index, ref in enumerate(group_by)}
        order_by: list[OrderTerm] = []
        for index, raw_order in enumerate(raw.order_by):
            if raw_order.ref.qualifier is None and raw_order.ref.name in output_positions:
                order_by.append(OrderTerm("output", raw_order.direction, output_name=raw_order.ref.name))
                continue
            ref = self._resolve(raw_order.ref, f"order_by[{index}]")
            if ref.text not in group_positions:
                raise _semantic_error(
                    f"ORDER BY reference {ref.text} is neither an output alias nor a GROUP BY column",
                    f"{self.path}.order_by[{index}]",
                )
            order_by.append(
                OrderTerm(
                    "group",
                    raw_order.direction,
                    group_index=group_positions[ref.text],
                )
            )

        return BoundQuery(
            raw.from_table,
            raw.from_alias,
            raw.join_table,
            raw.join_alias,
            tuple(join_pairs),
            tuple(predicates),
            tuple(select),
            tuple(group_by),
            tuple(order_by),
        )


def bind_query(raw: RawQuery, tables: Mapping[str, Table], *, path: str = "$.query") -> BoundQuery:
    return Binder(raw, tables, path).bind()
